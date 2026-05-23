/*
 * Copyright 2026 Rob Meades
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/** @file
 * @brief Implementation of the OTA API for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"
#include "esp_http_client.h"
#include "esp_flash_partitions.h"
#include "esp_partition.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "errno.h"

#include "fgr_metrics.h"
#include "fgr_monitor.h"
#include "fgr_nvs.h"
#include "fgr_ota.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "ota"

 // OTA buffer
#define OTA_BUFFER_SIZE 1024

// Downloaded file header buffer
#define OTA_FILE_HEADER_BUFFER_SIZE 8192

// SHA-256 digest length
#define HASH_LEN 32

// OTA URL buffer
#define OTA_URL_SIZE 256

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// The CA certificate for the OTA update server.
extern const char g_server_cert_pem_start[] asm("_binary_ca_cert_pem_start");

// An OTA data write buffer ready to write to the flash
static char g_ota_write_data[OTA_BUFFER_SIZE + 1] = { 0 };

// Buffer for accumulating header data
static char g_header_buffer[OTA_FILE_HEADER_BUFFER_SIZE] = { 0 };

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

static void print_sha256 (const uint8_t *image_hash, const char *label)
{
    char hash_print[HASH_LEN * 2 + 1];
    hash_print[HASH_LEN * 2] = 0;
    for (int32_t i = 0; i < HASH_LEN; ++i) {
        sprintf(&hash_print[i * 2], "%02x", image_hash[i]);
    }
    ESP_LOGI(TAG, "%s: %s", label, hash_print);
}

static void http_cleanup(esp_http_client_handle_t client)
{
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
}

// Check if we have enough data to parse the firmware header
//
// The ESP32 firmware header consists of:
//   - esp_image_header_t (8 bytes)
//   - esp_image_segment_header_t (8 bytes)
//   - esp_app_desc_t (256 bytes)
//
// Total: 272 bytes minimum
static bool has_complete_header(size_t accumulated_size)
{
    // We need at least the image header + first segment header + app descriptor
    size_t header_min_size = sizeof(esp_image_header_t) +
                             sizeof(esp_image_segment_header_t) +
                             sizeof(esp_app_desc_t);
    size_t header_safe_size = header_min_size + 1024; // Safety margin

    return accumulated_size >= header_safe_size;
}

// Helper function to get minimum header size
static size_t has_complete_header_min_size(void)
{
    return sizeof(esp_image_header_t) +
           sizeof(esp_image_segment_header_t) +
           sizeof(esp_app_desc_t) + 1024;  // + safety margin
}

// Parse and validate firmware header from accumulated buffer
// return 1 if OTA should proceed, 0 if no update needed, -1 on error
static int32_t parse_firmware_header(const char *buffer, size_t buffer_len,
                                     const esp_partition_t *running,
                                     esp_ota_handle_t *update_handle,
                                     const esp_partition_t *update_partition)
{
    esp_app_desc_t new_app_info;

    // Extract app description from the accumulated data
    memcpy(&new_app_info,
           &buffer[sizeof(esp_image_header_t) + sizeof(esp_image_segment_header_t)],
           sizeof(esp_app_desc_t));


    ESP_LOGI(TAG, "sizeof(esp_image_header_t): %d, sizeof(esp_image_segment_header_t) %d, sizeof(esp_app_desc_t) %d", sizeof(esp_image_header_t),
             sizeof(esp_image_segment_header_t), sizeof(esp_app_desc_t));
    ESP_LOGI(TAG, "New firmware version: %s.", new_app_info.version);

    esp_app_desc_t running_app_info;
    if (esp_ota_get_partition_description(running, &running_app_info) == ESP_OK) {
        ESP_LOGI(TAG, "Running firmware version: %s.", running_app_info.version);
    }

    const esp_partition_t* last_invalid_app = esp_ota_get_last_invalid_partition();
    esp_app_desc_t invalid_app_info;
    if (last_invalid_app != NULL &&
        esp_ota_get_partition_description(last_invalid_app, &invalid_app_info) == ESP_OK) {
        ESP_LOGI(TAG, "Last invalid firmware version: %s.", invalid_app_info.version);
    }

    // Check if this is the same as the last invalid version
    if (last_invalid_app != NULL) {
        if (memcmp(invalid_app_info.version, new_app_info.version, sizeof(new_app_info.version)) == 0) {
            ESP_LOGW(TAG, "New version is the same as previously invalid version.");
            ESP_LOGW(TAG, "The firmware with version %s previously failed to boot.", invalid_app_info.version);
            ESP_LOGW(TAG, "To prevent boot loop, we will not install this version.");
            return -1;  // This is still an error - don't install known bad version
        }
    }

    // Check if this is the same as the currently running version
    if (memcmp(new_app_info.version, running_app_info.version, sizeof(new_app_info.version)) == 0) {
        ESP_LOGW(TAG, "Current running version is the same as the new version.");
        ESP_LOGW(TAG, "No update needed - already running %s.", running_app_info.version);
        return 0;  // Success, but no update needed
    }

    // New valid version - begin OTA operation
    esp_err_t err = esp_ota_begin(update_partition, OTA_WITH_SEQUENTIAL_WRITES, update_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed (%s).", esp_err_to_name(err));
        return -1;
    }
    ESP_LOGI(TAG, "esp_ota_begin succeeded.");

    // Write any accumulated data that hasn't been written yet
    if (buffer_len > 0) {
        err = esp_ota_write(*update_handle, (const void *)buffer, buffer_len);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_write failed for accumulated data.");
            esp_ota_abort(*update_handle);
            return -1;
        }
        ESP_LOGI(TAG, "Wrote accumulated %d bytes to flash.", buffer_len);
    }

    return 1;  // Update in progress
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise OTA.
int32_t fgr_ota_init()
{
    uint8_t sha_256[HASH_LEN] = { 0 };
    esp_partition_t partition;

    // Get sha256 digest for the partition table
    partition.address   = ESP_PARTITION_TABLE_OFFSET;
    partition.size      = ESP_PARTITION_TABLE_MAX_LEN;
    partition.type      = ESP_PARTITION_TYPE_DATA;
    esp_partition_get_sha256(&partition, sha_256);
    print_sha256(sha_256, "SHA-256 for the partition table: ");

    // Get sha256 digest for bootloader
    partition.address   = ESP_BOOTLOADER_OFFSET;
    partition.size      = ESP_PARTITION_TABLE_OFFSET;
    partition.type      = ESP_PARTITION_TYPE_APP;
    esp_partition_get_sha256(&partition, sha_256);
    print_sha256(sha_256, "SHA-256 for bootloader: ");

    // Get sha256 digest for running partition
    esp_partition_get_sha256(esp_ota_get_running_partition(), sha_256);
    print_sha256(sha_256, "SHA-256 for current firmware: ");

    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t ota_state;
    if (esp_ota_get_state_partition(running, &ota_state) == ESP_OK) {
        if (ota_state == ESP_OTA_IMG_PENDING_VERIFY) {
            ESP_LOGI(TAG, "No diagnostic, continuing execution ...");
            esp_ota_mark_app_valid_cancel_rollback();
        }
    }

    // Initialize NVS
    return fgr_nvs_init();
}

// Perform an OTA update, requires networking to have been established.
int32_t fgr_ota_update(const char *update_file_url,
                       const char *server_cert_pem,
                       int32_t timeout_ms)
{
    esp_err_t err = ESP_OK;
    // update handle: set by esp_ota_begin(), must be freed via esp_ota_end()
    esp_ota_handle_t update_handle = 0 ;
    const esp_partition_t *update_partition = NULL;

    ESP_LOGI(TAG, "Starting OTA");

    const esp_partition_t *configured = esp_ota_get_boot_partition();
    const esp_partition_t *running = esp_ota_get_running_partition();

    if (configured != running) {
        ESP_LOGW(TAG, "Configured OTA boot partition at offset 0x%08"PRIx32", but running from offset 0x%08"PRIx32".",
                 configured->address, running->address);
        ESP_LOGW(TAG, "(This can happen if either the OTA boot data or preferred boot image become corrupted somehow.)");
    }
    ESP_LOGI(TAG, "Running partition type %d subtype %d (offset 0x%08"PRIx32").",
             running->type, running->subtype, running->address);

    esp_http_client_config_t config = {
        .url = update_file_url,
        .cert_pem = g_server_cert_pem_start,
        .timeout_ms = timeout_ms,
        .keep_alive_enable = true,
        .buffer_size = 2048
    };

    // Begin the HTTP download
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client != NULL) {
        err = esp_http_client_open(client, 0);
        if (err == ESP_OK) {
            int content_length = esp_http_client_fetch_headers(client);
            ESP_LOGI(TAG, "Content-Length: %d.", content_length);
        } else {
            ESP_LOGE(TAG, "Failed to open HTTP connection: %s.", esp_err_to_name(err));
        }
    } else {
        ESP_LOGE(TAG, "Failed to initialise HTTP connection.");
        err = ESP_ERR_NO_MEM;
    }

    // Get the file
    int32_t binary_file_length = 0;
    if (err == ESP_OK) {
        update_partition = esp_ota_get_next_update_partition(NULL);
        assert(update_partition != NULL);
        ESP_LOGI(TAG, "Writing to partition subtype %d at offset 0x%"PRIx32".",
                 update_partition->subtype, update_partition->address);

        bool image_header_was_checked = false;
        size_t header_accumulated = 0;
        int32_t zero_read_count = 0;
        int32_t max_zero_reads = 10;  // Maximum consecutive zero reads before considering connection dead

        // State variables to control the loop
        bool transfer_complete = false;
        bool same_version_detected = false;

        // Deal with all receive packets
        while (err == ESP_OK && !transfer_complete && !same_version_detected) {
            int32_t data_read = esp_http_client_read(client, g_ota_write_data, OTA_BUFFER_SIZE);
            if (data_read < 0) {
                ESP_LOGE(TAG, "Error: SSL data read error.");
                err = ESP_ERR_INVALID_RESPONSE;
            } else if (data_read > 0) {
                // Reset zero read counter on successful read
                zero_read_count = 0;
                if (image_header_was_checked == false) {
                    // Accumulate data until we have enough to parse the header
                    if (header_accumulated + data_read <= OTA_FILE_HEADER_BUFFER_SIZE) {
                        memcpy(g_header_buffer + header_accumulated, g_ota_write_data, data_read);
                        header_accumulated += data_read;
                        ESP_LOGI(TAG, "Accumulated %d/%d bytes for header.", header_accumulated, OTA_FILE_HEADER_BUFFER_SIZE);
                    } else {
                        ESP_LOGE(TAG, "Header buffer overflow!");
                        err = ESP_ERR_NO_MEM;
                    }
                    /* Check if we have accumulated enough data to parse the header */
                    if (has_complete_header(header_accumulated)) {
                        ESP_LOGI(TAG, "Complete header accumulated (%d byte(s)).", header_accumulated);
                        int32_t parse_result = parse_firmware_header(g_header_buffer, header_accumulated,
                                                                     running, &update_handle, update_partition);
                        if (parse_result == 1) {
                            // Update in progress
                            image_header_was_checked = true;
                            binary_file_length = header_accumulated;
                            ESP_LOGI(TAG, "New version detected, continuing with OTA...");
                            // Will continue to next iteration naturally
                        } else if (parse_result == 0) {
                            // Same version - no update needed
                            ESP_LOGI(TAG, "Already running latest version, no update needed.");
                            // Set flag to exit loop and go to infinite loop
                            same_version_detected = true;
                        } else { /* parse_result == -1 */
                            // Fatal error (like invalid version that previously failed)
                            ESP_LOGE(TAG, "Firmware header validation failed.");
                            err = ESP_ERR_INVALID_RESPONSE;
                        }
                    } else {
                        ESP_LOGI(TAG, "Still accumulating header data (need more than %zu byte(s), currently at %zu).",
                                 has_complete_header_min_size(), header_accumulated);
                        // Need to keep accumulating - will continue naturally
                    }
                } else {
                    // Header already processed, write data directly
                    err = esp_ota_write(update_handle, (const void *)g_ota_write_data, data_read);
                    if (err != ESP_OK) {
                        ESP_LOGE(TAG, "esp_ota_write failed.");
                    }
                    binary_file_length += data_read;
                }
                // Feed the watchdog
                fgr_monitor_task_wdt_feed(NULL);
            } else if (data_read == 0) {
                // Handle zero read - this could be temporary or permanent
                zero_read_count++;
                ESP_LOGI(TAG, "Zero read #%d, header_accumulated=%d, header_checked=%d.",
                         zero_read_count, header_accumulated, image_header_was_checked);
                // If we haven't checked the header yet, we need to be careful
                if (!image_header_was_checked) {
                    // We haven't accumulated enough header data yet - this zero read might be temporary
                    if (esp_http_client_is_complete_data_received(client) == true) {
                        // Server says transfer is complete, but we don't have enough header data
                        ESP_LOGE(TAG, "Connection closed before accumulating enough header data!");
                        ESP_LOGE(TAG, "Accumulated only %zu bytes, need at least %zu.",
                                 header_accumulated, has_complete_header_min_size());
                        err = ESP_ERR_NOT_FINISHED;
                    } else if (zero_read_count >= max_zero_reads) {
                        // Too many consecutive zero reads - connection might be dead
                        ESP_LOGE(TAG, "Too many zero reads (%d) while accumulating header.", zero_read_count);
                        err = ESP_ERR_INVALID_RESPONSE;
                    } else {
                        // Temporary zero read - wait a bit and continue
                        ESP_LOGI(TAG, "Temporary zero read during header accumulation, waiting...");
                        vTaskDelay(pdMS_TO_TICKS(100));
                        // Will continue naturally
                    }
                } else {
                    // We've already processed the header, now handle zero reads normally
                    if (errno == ECONNRESET || errno == ENOTCONN) {
                        ESP_LOGE(TAG, "Connection closed, errno = %d.", errno);
                        err = ESP_ERR_NOT_FINISHED;
                    }
                    else if (esp_http_client_is_complete_data_received(client) == true) {
                        ESP_LOGI(TAG, "Connection closed - transfer complete.");
                        transfer_complete = true;
                    }
                    else if (zero_read_count >= max_zero_reads) {
                        ESP_LOGE(TAG, "Too many zero reads (%d) after header, connection may be dead.", zero_read_count);
                        err = ESP_ERR_INVALID_RESPONSE;
                    }
                    else {
                        // Small delay to avoid busy-looping when no data available
                        ESP_LOGI(TAG, "Temporary zero read, waiting for more data...");
                        vTaskDelay(pdMS_TO_TICKS(50));
                        // Will continue naturally
                    }
                }
            }
        }

        if ((err != ESP_OK) && !same_version_detected) {
            // Download failed and it wasn't because we dropped it because the
            // downloading file was the same version as we are running
            fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_OTA_CONNECTION, false, 0);
        }
    } else {
        // Connection failed
        fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_OTA_CONNECTION, false, 0);
    }

    // Either have a new binary file or don't need one, finish the OTA
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Total write binary data length: %d.", binary_file_length);
        if (esp_http_client_is_complete_data_received(client)) {
            err = esp_ota_end(update_handle);
            update_handle = 0;
            if (err == ESP_OK) {
                err = esp_ota_set_boot_partition(update_partition);
                if (err == ESP_OK) {
                    ESP_LOGI(TAG, "Prepare to restart system!");
                    fgr_metrics_event_set(FGR_METRIC_EVENT_LOCAL_REBOOT, 0);
                    esp_restart();
                }
            } else {
                if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
                    ESP_LOGE(TAG, "Image validation failed, image is corrupted.");
                } else {
                    ESP_LOGE(TAG, "esp_ota_end failed (%s)!", esp_err_to_name(err));
                }
            }
            fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_OTA_NVS_WRITE,
                                       err == ESP_OK,
                                       binary_file_length);
        }
    }

    // Clean-up
    if (client) {
        http_cleanup(client);
    }
    if (update_handle) {
        esp_ota_abort(update_handle);
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// End of file

