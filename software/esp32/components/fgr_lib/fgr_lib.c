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
 * @brief Implementation of general library functions for the front
 * garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <inttypes.h>
#include "esp_system.h"
#include "esp_task_wdt.h"

#include "fgr_util.h"
#include "fgr_task.h"
#include "fgr_monitor.h"
#include "fgr_time.h"
#include "fgr_nvs.h"
#include "fgr_ota.h"
#include "fgr_network.h"
#include "fgr_debug.h"
#include "fgr_metrics.h"
#include "fgr_msg.h"
#include "fgr_log.h"

#include "fgr_lib.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "lib"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Keep track of whether we've been called.
static bool g_called = false;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialize all libraries.
int32_t fgr_lib_init(const char *ota_server_cert_pem,
                     fgr_msg_state_cb_t state_cb,
                     fgr_msg_send_cb_t send_cb,
                     fgr_util_cb_t restart_cb,
                     fgr_ota_app_is_good_cb_t app_is_good_cb,
                     void *cb_param)
{
    int32_t err = ESP_OK;

    if (!g_called) {
        g_called = true;

        // Allow us to feed the watchdog
        esp_task_wdt_add(NULL);

#if defined(FGR_LIB_INITIALISATION_DELAY_SECONDS) && (FGR_LIB_INITIALISATION_DELAY_SECONDS > 0)
        ESP_LOGI(TAG, "Pausing for %d second(s).", FGR_LIB_INITIALISATION_DELAY_SECONDS);
        vTaskDelay(pdMS_TO_TICKS(FGR_LIB_INITIALISATION_DELAY_SECONDS * 1000));
        fgr_monitor_task_wdt_feed(NULL);
#endif

        // Initialise tasking
        err = fgr_task_init();

        // Configure monitoring: monitors tasks so has to come after
        // fgr_task_init()
        if (err == ESP_OK) {
            err = fgr_monitor_init(restart_cb, cb_param);
        }

        // Configure metrics: needs tasks so has to come after
        // fgr_task_init()
        if (err == ESP_OK) {
            err = fgr_metrics_init(fgr_metrics_log_cb, NULL);
        }

        // Initialise OTA: do this whether there is WiFi or not
        // as it also initialises non-volatile storage (and you
        // can't just separately initialise non-volatile storage
        // as there are some OTA-related steps that need to be
        // performed beforehand)
        if (err == ESP_OK) {
            err = fgr_ota_init(fgr_msg_is_connected, fgr_log_is_connected,
                               app_is_good_cb, restart_cb, cb_param);
        }

        // Configure our debug LED: do this after non-volatile
        // storage has been initialised so that we can read
        // settings from there.
        if (err == ESP_OK) {
            err = fgr_debug_init(state_cb, cb_param);
        }

#if !defined(CONFIG_FGR_APP_NO_WIFI)
        // Initialize networking
        if (err == ESP_OK) {
            err = fgr_network_init(CONFIG_FGR_NETWORK_WIFI_SSID,
                                   CONFIG_FGR_NETWORK_WIFI_PASSWORD,
                                   WIFI_AUTH_OPEN,
                                   CONFIG_FGR_NETWORK_WIFI_REDUCED_TX_POWER);
        }

        // Establish absolute time
        if (err == ESP_OK) {
            err = fgr_time_init(FGR_TIME_NTP_SERVER_IP_ADDRESS,
                                FGR_TIME_TIMEZONE_LONDON,
                                FGR_TIME_NTP_SYNC_INTERVAL_SECONDS);
        }

        // Check for an OTA update, which may restart the system
        if (err == ESP_OK) {
            err = fgr_ota_update(CONFIG_FGR_OTA_FIRMWARE_UPGRADE_URL,
                                 ota_server_cert_pem,
                                 CONFIG_FGR_OTA_RECEIVE_TIMEOUT_MS);
        }

        // Forward logging to the server
        if (err == ESP_OK) {
            err = fgr_log_init(CONFIG_FGR_NETWORK_CONTROLLER_IP_ADDRESS,
                               CONFIG_FGR_LOG_PORT, FGR_LOG_LEVEL_INFO);
        }

        // Now that we have a connection to a log server,
        // if there was previously a panic resulting in a backtrace,
        // or a stack overflow, and maybe an associated core dump,
        // log the lot
        // IMPORTANT: the tags used below are parsed out by log_server.py
        // when writing to database, so if you change them you will need to
        // change that script also.
        if (err == ESP_OK) {
            fgr_monitor_abort_reason_log("ABORT", NULL, ESP_LOG_WARN);
            fgr_debug_panic_log("BACKTRACE", NULL, ESP_LOG_WARN);
            fgr_debug_stack_overflow_log("STACK_OVERFLOW", NULL, ESP_LOG_WARN);
            fgr_debug_core_dump_get("CORE_DUMP", ESP_LOG_INFO);
        }

        // Initialise messaging
        if (err == ESP_OK) {
            err = fgr_msg_init(CONFIG_FGR_NETWORK_CONTROLLER_IP_ADDRESS,
                               CONFIG_FGR_MSG_PORT,
                               CONFIG_FGR_MSG_HEARTBEAT_SECONDS,
                               state_cb, cb_param);
            if (err == ESP_OK) {
                // Let monitor know when a message is received (any message)
                err = fgr_msg_receive_cb_set(fgr_monitor_msg_receive_cb, NULL);
            }
            if (err == ESP_OK) {
                // Allow msg access to RSSI (so that it is included in heartbeats)
                err = fgr_msg_rssi_cb_set(fgr_metrics_rssi_get, NULL);
            }
            if (err == ESP_OK) {
                // Add the logging received message handler
                err = fgr_msg_receive_handler_add(0, fgr_log_msg_receive_handler_cb, NULL);
            }
            if (err == ESP_OK) {
                // Add the debug received message handler
                err = fgr_msg_receive_handler_add(0, fgr_debug_msg_receive_handler_cb, NULL);
            }
        }

        // For debug purposes, hook-in a message send callback
        if (err == ESP_OK) {
            err = fgr_msg_send_cb_set(send_cb, cb_param);
        }

        // Create a message send queue
        if (err == ESP_OK) {
            err = fgr_msg_send_queue_init(FGR_MSG_SEND_QUEUE_LENGTH);
        }

#else
        ESP_LOGW(TAG, "CONFIG_FGR_APP_NO_WIFI is defined, not connecting to WiFi.");
#endif
    }

    return err;
}

// Deinitialise all libraries.
void fgr_lib_deinit(void)
{
    if (g_called) {
        fgr_msg_deinit();
        fgr_log_deinit();
        fgr_time_deinit();
        fgr_network_deinit();
        fgr_debug_deinit();
        fgr_metrics_deinit();
        fgr_task_deinit();

        esp_task_wdt_delete(NULL);

        g_called = false;
    }
}

// End of file

