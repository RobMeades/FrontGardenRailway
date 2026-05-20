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
 * @brief Implementation of the networking API for a node of the front
 * garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "esp_event.h"
#include "esp_log.h"
#include "errno.h"
#include "esp_wifi.h"
#include "esp_wifi_types.h"
#include "esp_phy_init.h"
#include "lwip/netdb.h"
#include "esp_task_wdt.h"

#include "fgr_metrics.h"
#include "fgr_network.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "network"

#ifndef FGR_NETWORK_IP_ADDRESS_ASSIGNMENT_WAIT_SECONDS
// How long to wait for an IP address to be assigned, in seconds;
// Should not be more than 60 seconds since that is the maximum
// duration of the watchdog timer.
#  define FGR_NETWORK_IP_ADDRESS_ASSIGNMENT_WAIT_SECONDS 30
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Semaphore for WiFi synchronization
static SemaphoreHandle_t g_wifi_semaphore = NULL;

// Wifi network interface
static esp_netif_t *g_sta_netif = NULL;

// Whether Wifi has successfully connected or not
static bool is_connected = false;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: EVENT HANDLERS
 * -------------------------------------------------------------- */

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        is_connected = false;
        ESP_LOGI(TAG, "WiFi started, connecting...");
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
        fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_WIFI_CONNECTION, true, 0);
        ESP_LOGI(TAG, "WiFi connected to AP.");
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        is_connected = false;
        wifi_event_sta_disconnected_t *event = (wifi_event_sta_disconnected_t*) event_data;
        fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_WIFI_CONNECTION, false, 0);
        ESP_LOGI(TAG, "WiFi disconnected, reason: %d.", event->reason);

        // Print the AP info that failed
        ESP_LOGI(TAG, "AP SSID: %.32s.", event->ssid);
        ESP_LOGI(TAG, "AP BSSID: %02x:%02x:%02x:%02x:%02x:%02x.",
                event->bssid[0], event->bssid[1], event->bssid[2],
                event->bssid[3], event->bssid[4], event->bssid[5]);
        ESP_LOGI(TAG, "Reason: %d.", event->reason);

        ESP_LOGI(TAG, "Attempting to reconnect...");
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_AUTHMODE_CHANGE) {
        wifi_event_sta_authmode_change_t *event = (wifi_event_sta_authmode_change_t*) event_data;
        ESP_LOGI(TAG, "Authmode changed from %d to %d.",
                 event->old_mode, event->new_mode);
    }
}

static void ip_event_handler(void *arg, esp_event_base_t event_base,
                             int32_t event_id, void *event_data)
{
    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *) event_data;
        is_connected = true;
        fgr_metrics_event_set(FGR_METRIC_EVENT_IP_CONNECTION, 0);
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip), ".");

        SemaphoreHandle_t semaphore = (SemaphoreHandle_t) arg;
        if (semaphore != NULL) {
            xSemaphoreGive(semaphore);
        }
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise networking.
int32_t fgr_network_init(const char *ssid, const char *password,
                         wifi_auth_mode_t auth_mode,
                         bool reduced_tx_power)
{
    esp_err_t err = ESP_ERR_INVALID_ARG;
    wifi_config_t wifi_config = {0};

    // Continue if there is no password or, if there is a
    // password, the authentication mode is not "open",
    // also if ssid and password are within length
    if ((((password == NULL) || (strlen(password) == 0)) ||
         (auth_mode != WIFI_AUTH_OPEN)) &&
         ((ssid != NULL) && (strlen(ssid) < sizeof(wifi_config.sta.ssid))) &&
         ((password == NULL) || (strlen(password) < sizeof(wifi_config.sta.password))))  {

        // Initialize network interface
        err = esp_netif_init();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to initialize network interface: %s.", esp_err_to_name(err));
        }

        // Create semaphore for WiFi synchronization
        if (err == ESP_OK && g_wifi_semaphore == NULL) {
            g_wifi_semaphore = xSemaphoreCreateBinary();
            if (g_wifi_semaphore == NULL) {
                ESP_LOGE(TAG, "Failed to create WiFi semaphore.");
                err = ESP_ERR_NO_MEM;
            }
        }

        // Create default WiFi station
        if (err == ESP_OK) {
            g_sta_netif = esp_netif_create_default_wifi_sta();
            if (g_sta_netif == NULL) {
                ESP_LOGE(TAG, "Failed to create default WiFi station.");
                err = ESP_ERR_NO_MEM;
            }
        }

        // Initialize WiFi
        if (err == ESP_OK) {
            wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
            err = esp_wifi_init(&cfg);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to initialize WiFi: %s.", esp_err_to_name(err));
            }
        }

        // Register event handlers for WiFi and IP
        if (err == ESP_OK) {
            err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                             &wifi_event_handler, NULL);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to register WiFi event handler: %s.", esp_err_to_name(err));
            }
        }
        if (err == ESP_OK) {
            err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                            &ip_event_handler, g_wifi_semaphore);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to register IP event handler: %s.", esp_err_to_name(err));
            }
        }

        // Set WiFi to station mode
        if (err == ESP_OK) {
            err = esp_wifi_set_mode(WIFI_MODE_STA);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to set WiFi mode: %s.", esp_err_to_name(err));
            }
        }

        // Configure WiFi connection
        if (err == ESP_OK) {
            strncpy((char *) wifi_config.sta.ssid, ssid, sizeof(wifi_config.sta.ssid));
            if (password != NULL && (strlen(password) > 0)) {
                strncpy((char *) wifi_config.sta.password, password, sizeof(wifi_config.sta.password));
            }
            wifi_config.sta.threshold.authmode = auth_mode;
            wifi_config.sta.pmf_cfg.capable = true;
            wifi_config.sta.pmf_cfg.required = false;

            err = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to set WiFi configuration: %s.", esp_err_to_name(err));
            }
        }

        // Start WiFi
        if (err == ESP_OK) {
            err = esp_wifi_start();
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to start WiFi: %s.", esp_err_to_name(err));
            } else {
                ESP_LOGI(TAG, "WiFi connecting to %s...", ssid);
            }
        }

        // Disable WiFi power save mode for best OTA operation
        if (err == ESP_OK) {
            esp_err_t ps_err = esp_wifi_set_ps(WIFI_PS_NONE);
            if (ps_err != ESP_OK) {
                ESP_LOGW(TAG, "Failed to disable WiFi power save: %s.", esp_err_to_name(ps_err));
                // Continue anyway, this is not fatal
            }
        }

        if (reduced_tx_power) {
            // Set max TX power to 8 dBm
            if (err == ESP_OK) {
                // The parameter to esp_wifi_set_max_tx_power() is in quarters of a dB
                esp_err_t ps_err = esp_wifi_set_max_tx_power(34);
                if (ps_err != ESP_OK) {
                    ESP_LOGW(TAG, "Unable to set TX max power to 8 dBm: %s.", esp_err_to_name(ps_err));
                    // Continue anyway, this is not fatal
                }
            }
        }

        // Wait to connect and obtain IP address (with timeout)
        if (err == ESP_OK) {
            // Set the watchdog timer to max (60 seconds) while we do this
            bool watchdog_running = false;
            esp_task_wdt_config_t wdt_cfg = {
                .timeout_ms = 60 * 1000,
#if defined CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0
                .idle_core_mask = 1 << CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0,
#else
                .idle_core_mask = 0,
#endif
#if defined CONFIG_ESP_TASK_WDT_PANIC
                .trigger_panic = true
#else
                .trigger_panic = false
#endif
            };
            if (esp_task_wdt_status(NULL) == ESP_OK) {
                watchdog_running = true;
                esp_task_wdt_reconfigure(&wdt_cfg);
            }
            ESP_LOGI(TAG, "Waiting %d second(s) to connect and be assigned IP address.",
                     FGR_NETWORK_IP_ADDRESS_ASSIGNMENT_WAIT_SECONDS);
            if (xSemaphoreTake(g_wifi_semaphore,
                               pdMS_TO_TICKS(FGR_NETWORK_IP_ADDRESS_ASSIGNMENT_WAIT_SECONDS * 1000)) == pdTRUE) {
                ESP_LOGI(TAG, "WiFi connected, IP obtained.");
            } else {
                ESP_LOGE(TAG, "Failed to obtain IP address within timeout.");
                err = ESP_ERR_TIMEOUT;
            }
            if (watchdog_running) {
                // Put the watchdog timer back
                wdt_cfg.timeout_ms = CONFIG_ESP_TASK_WDT_TIMEOUT_S * 1000;
                esp_task_wdt_reconfigure(&wdt_cfg);
            }
        }

        // Clean-up on error
        if (err != ESP_OK) {
            if (g_sta_netif) {
                esp_netif_destroy_default_wifi(g_sta_netif);
                g_sta_netif = NULL;
                // Can't clean up g_wifi_semaphore as it may still be taken
            }
        }
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Deinitialise networking.
void fgr_network_deinit()
{
    if (g_sta_netif != NULL) {
        esp_netif_destroy_default_wifi(g_sta_netif);
        g_sta_netif = NULL;
    }
}

// Determine if networking is connected.
bool fgr_network_is_connected()
{
    return is_connected;
}

// Return the hostname part of a URL.
size_t fgr_network_hostname_from_url(const char *url, char *buffer, size_t length)
{
    size_t returned_length = 0;
    char *hostname_start = strstr(url, "//");
    size_t buffer_used = 0;

    if (hostname_start && length > 0) {
        hostname_start += 2;
        for (char *p = hostname_start;
                (*p != 0) && (*p != '/') && (*p != ':');
                p++) {
            if (buffer_used < length - 1) {
                buffer[buffer_used] = *p;
                buffer_used++;
            }
            returned_length++;
        }
        // Add terminator
        buffer[buffer_used] = 0;
    }

    return returned_length;
}

// End of file

