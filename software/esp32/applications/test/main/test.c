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
 * @brief A test node for the front garden railway.
 */

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "esp_event.h"
#include "esp_log.h"
#include "errno.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"

#include "../../../../protocol/fgr_protocol.h"
#include "fgr_util.h"
#include "fgr_ota.h"
#include "fgr_network.h"
#include "fgr_socket.h"
#include "fgr_msg.h"
#include "fgr_debug.h"
#include "fgr_log.h"
#include "fgr_ping.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "test"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// The CA certificate for the OTA update server.
extern const uint8_t g_server_cert_pem_start[] asm("_binary_ca_cert_pem_start");

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Generic initialisation.
esp_err_t init(void)
{
    // Print out our Wi-Fi MAC address
    fgr_debug_print_mac_address();

    // Create the default event loop, for everyone's use
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create default event loop: %s.", esp_err_to_name(err));
    }

    // Configure our debug LED
    if (err == ESP_OK) {
        err = fgr_debug_init();
    }

#if !defined(CONFIG_FGR_APP_NO_WIFI)
    // Initialise OTA
    if (err == ESP_OK) {
        err = fgr_ota_init();
    }

    // Initialize networking
    if (err == ESP_OK) {
        err = fgr_network_init(CONFIG_FGR_NETWORK_WIFI_SSID, CONFIG_FGR_NETWORK_WIFI_PASSWORD, WIFI_AUTH_OPEN);
    }

    // Check for an OTA update, which may restart the system
    if (err == ESP_OK) {
        err = fgr_ota_update(CONFIG_FGR_OTA_FIRMWARE_UPGRADE_URL,
                             (const char *) g_server_cert_pem_start,
                             CONFIG_FGR_OTA_RECEIVE_TIMEOUT_MS);
    }

    // Forward logging to the server
    if (err == ESP_OK) {
        err = fgr_log_init(CONFIG_FGR_NETWORK_CONTROLLER_IP_ADDRESS, CONFIG_FGR_LOG_PORT, FGR_LOG_LEVEL_INFO);
    }

    // Initialise messaging
    //if (err == ESP_OK) {
    //    err = fgr_msg_init(CONFIG_FGR_NETWORK_CONTROLLER_IP_ADDRESS, CONFIG_FGR_MSG_PORT);
    //}

#else
    ESP_LOGW(TAG, "CONFIG_FGR_APP_NO_WIFI is defined, not connecting to WiFi.");
#endif

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Entry point.
void app_main(void)
{
    ESP_LOGI(TAG, "app_main start.");

    int32_t err = init();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Initialization complete.");

        // Allow us to feed the watchdog
        esp_task_wdt_add(NULL);
        while(1) {
            ESP_LOGI(TAG, "Test node idle.");
            vTaskDelay(pdMS_TO_TICKS(4000));
            esp_task_wdt_reset();
        }
        esp_task_wdt_delete(NULL);

    } else {
        ESP_LOGE(TAG, "Initialization failed, system cannot continue, will restart soonish.");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }

    fgr_log_deinit();
    fgr_network_deinit();
    esp_restart();
}

// End of file
