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
 * @brief Implementation of logging that includes forwarding
 * to the controller for a node of the front garden railway.
 */

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_log.h"
#include "errno.h"

#include "fgr_util.h"
#include "fgr_socket.h"
#include "fgr_log.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "log"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Context.
typedef struct {
    int sock;
    void *context_sock;
    bool connected;
    SemaphoreHandle_t lock;
    fgr_log_level_t min_level;  // Minimum level to forward
} log_cfg_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static log_cfg_t g_log_cfg = {
    .sock = -1,
    .min_level = FGR_LOG_LEVEL_INFO  // Default: forward INFO and above
};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Custom vprintf handler with level filtering
static int tcp_log_vprintf(const char *fmt, va_list args)
{
    fgr_msg_t log_msg;
    fgr_msg_header_log_t *header = &(log_msg.header.log);
    fgr_msg_body_t *body = &(log_msg.body);

    // Parse the log level from format string
    // ESP-IDF logs start with level character: "I (123) TAG: message"
    esp_log_level_t esp_log_level = ESP_LOG_INFO;
    if (fmt[0] == 'E') {
        esp_log_level = ESP_LOG_ERROR;
    } else if (fmt[0] == 'W') {
        esp_log_level = ESP_LOG_WARN;
    } else if (fmt[0] == 'I') {
        esp_log_level = ESP_LOG_INFO;
    } else if (fmt[0] == 'D') {
        esp_log_level = ESP_LOG_DEBUG;
    } else if (fmt[0] == 'V') {
        esp_log_level = ESP_LOG_VERBOSE;
    }

    // Convert to protocol log level
    fgr_log_level_t fgr_log_level = FGR_LOG_LEVEL_INFO;
    switch(esp_log_level) {
        case ESP_LOG_ERROR:
            fgr_log_level = FGR_LOG_LEVEL_ERROR;
            break;
        case ESP_LOG_WARN:
            fgr_log_level = FGR_LOG_LEVEL_WARN;
            break;
        case ESP_LOG_INFO:
            fgr_log_level = FGR_LOG_LEVEL_INFO;
            break;
        case ESP_LOG_DEBUG:
            fgr_log_level = FGR_LOG_LEVEL_DEBUG;
            break;
        case ESP_LOG_VERBOSE:
            fgr_log_level = FGR_LOG_LEVEL_DEBUG;
            break;
        default:
            break;
    }

    // Format the message
    int32_t length = vsnprintf((char *) body->contents, FGR_LOG_STRING_MAX_LEN, fmt, args);

    if (length > FGR_LOG_STRING_MAX_LEN) {
        length = FGR_LOG_STRING_MAX_LEN;
    }

    // Forward if level meets minimum and we're connected
    if ((length > 0) && g_log_cfg.connected && (fgr_log_level >= g_log_cfg.min_level)) {

        if (xSemaphoreTake(g_log_cfg.lock, pdMS_TO_TICKS(100)) == pdTRUE) {

            header->type = ((uint16_t) FGR_MSG_TYPE_LOG) << 12;
            header->level = fgr_log_level;
            body->length = (uint32_t) length;
            body->contents[length] = 0; // Ensure terminator

            if (fgr_socket_send(g_log_cfg.sock, (const uint8_t *) &log_msg, sizeof(log_msg), 0) != ESP_OK) {
                fgr_socket_channel_failed(&g_log_cfg.context_sock);
                g_log_cfg.connected = false;
            }

            xSemaphoreGive(g_log_cfg.lock);
        }
    }

    // Always output to UART for local debugging
    return vprintf(fmt, args);
}

// Callback called by fgr_socket_channel_maintain().
static void socket_reconnect_cb(int sock, void *param)
{
    log_cfg_t *log_cfg = (log_cfg_t *) param;

    if (log_cfg->lock) {
        // Nothing to do other than update the socket
        // since the previous has probably been closed
        // and set the connected flag back to true
        CONTEXT_LOCK(log_cfg->lock, "socket_reconnect_cb() 1");
        log_cfg->sock = sock;
        log_cfg->connected = true;
        CONTEXT_UNLOCK(log_cfg->lock, "socket_reconnect_cb() 1");
    }
}

// Wot it says.
static void clean_up()
{
    // Restore default logging
    esp_log_set_vprintf(vprintf);

    if (g_log_cfg.lock) {

        ESP_LOGI(TAG, "Stopping log forwarding.");

        CONTEXT_LOCK(g_log_cfg.lock, "clean_up() 1");

        // Lose the socket
        fgr_socket_channel_stop(&g_log_cfg.context_sock);
        g_log_cfg.sock = -1;

        CONTEXT_UNLOCK(g_log_cfg.lock, "clean_up() 1");
        // Don't delete the semaphore, someone might have it still
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialize logging.
int32_t fgr_log_init(const char *server_ip, uint16_t port,
                     fgr_log_level_t min_level)
{
    int32_t err = ESP_OK;

    if (g_log_cfg.sock < 0) {
        if (!g_log_cfg.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_log_cfg.lock = xSemaphoreCreateMutex();
        }

        if (g_log_cfg.lock) {

            CONTEXT_LOCK(g_log_cfg.lock, "fgr_log_init()");

            g_log_cfg.min_level = min_level;
            // Create connection to server
            err = fgr_socket_channel_start(server_ip, port,
                                           &g_log_cfg.sock,
                                           &g_log_cfg.context_sock);
            if (err == ESP_OK) {
                // Maintain the connection
                err = fgr_socket_channel_maintain(&g_log_cfg.context_sock,
                                                  socket_reconnect_cb,
                                                  &g_log_cfg);
                if (err != ESP_OK) {
                    fgr_socket_channel_stop(&g_log_cfg.context_sock);
                    g_log_cfg.sock = -1;
                }
            }

            CONTEXT_UNLOCK(g_log_cfg.lock, "fgr_log_init()");

            if (err == ESP_OK) {
                // Set vprintf handler
                g_log_cfg.connected = true;
                esp_log_set_vprintf(tcp_log_vprintf);
                ESP_LOGI(TAG, "Logs will be forwarded to %s:%d, log level %d.",
                         server_ip, port, g_log_cfg.min_level);

            }
        }
    }

    if (err != ESP_OK) {
        clean_up();
    }

    return err;
}

// Deinitialise logging
void fgr_log_deinit(void)
{
    clean_up();
}

// Set minimum log level to forward
int32_t fgr_log_set_min_level(fgr_log_level_t level)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_log_cfg.lock) {

        CONTEXT_LOCK(g_log_cfg.lock, "fgr_log_set_min_level");

        g_log_cfg.min_level = level;

        CONTEXT_UNLOCK(g_log_cfg.lock, "fgr_log_set_min_level");

        ESP_LOGI(TAG, "Log level set to %d.", g_log_cfg.min_level);
    }

    return err;
}

// End of file

