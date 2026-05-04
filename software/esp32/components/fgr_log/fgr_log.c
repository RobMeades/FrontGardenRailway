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
#include "arpa/inet.h"
#include "errno.h"

#include "fgr_util.h"
#include "fgr_socket.h"
#include "fgr_msg.h"
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
    bool on_not_off;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static context_t g_context = {
    .sock = -1,
    .min_level = FGR_LOG_LEVEL_INFO,  // Default: forward INFO and above
    .on_not_off = true
};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLBACKS
 * -------------------------------------------------------------- */

// Custom vprintf handler with level filtering
static int tcp_log_vprintf(const char *fmt, va_list args)
{
    fgr_msg_t log_msg;
    fgr_msg_header_log_t *header = &(log_msg.header.log);
    fgr_msg_body_t *body = &(log_msg.body);

    if (g_context.on_not_off) {
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
        int32_t length = vsnprintf((char *) (body->contents), FGR_LOG_STRING_MAX_LEN, fmt, args);

        if (length > FGR_LOG_STRING_MAX_LEN) {
            length = FGR_LOG_STRING_MAX_LEN;
        }

        // Strip off any trailing linefeed
        if ((length > 0) && (body->contents[length - 1] == '\n')) {
            length--;
        }

        // Forward if level meets minimum and we're connected
        if ((length > 0) && g_context.connected && (fgr_log_level >= g_context.min_level)) {

            if (xSemaphoreTake(g_context.lock, pdMS_TO_TICKS(100)) == pdTRUE) {

                header->type = htons(((uint16_t) FGR_MSG_TYPE_LOG) << 12);
                header->level = fgr_log_level;
                body->length = htonl((uint32_t) length);
                body->contents[length] = 0; // Ensure terminator

                if (fgr_socket_send(g_context.sock, (const uint8_t *) &log_msg,
                                    sizeof(log_msg.header) + sizeof(log_msg.body.length) + length, 0) == ESP_OK) {
                    fgr_socket_channel_activity(&g_context.context_sock);
                } else {
                    fgr_socket_channel_failed(&g_context.context_sock);
                    g_context.connected = false;
                }

                xSemaphoreGive(g_context.lock);
            }
        }
    }

    // Always output to UART for local debugging
    return vprintf(fmt, args);
}

// Callback to send a heartbeat log, called by
// fgr_socket_channel_maintain().
static void socket_heartbeat_cb(int sock, void *param)
{
    context_t *context = (context_t *) param;
    (void) sock;

    if (context->lock) {
        ESP_LOGI(TAG, "Log heartbeat.");
        CONTEXT_LOCK(context->lock, "socket_heartbeat_cb() log");
        fgr_socket_channel_activity(&context->context_sock);
        CONTEXT_UNLOCK(context->lock, "socket_heartbeat_cb() log");
    }
}

// Callback on socket reconnection, called by
// fgr_socket_channel_maintain().
static void socket_reconnect_cb(int sock, void *param)
{
    context_t *context = (context_t *) param;

    if (context->lock) {
        // Nothing to do other than update the socket
        // since the previous has probably been closed
        // and set the connected flag back to true
        CONTEXT_LOCK(context->lock, "socket_reconnect_cb() log");
        context->sock = sock;
        context->connected = true;
        CONTEXT_UNLOCK(context->lock, "socket_reconnect_cb() log");
    }
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Wot it says.
static void clean_up()
{
    // Restore default logging
    esp_log_set_vprintf(vprintf);

    if (g_context.lock) {

        ESP_LOGI(TAG, "Stopping log forwarding.");

        CONTEXT_LOCK(g_context.lock, "clean_up() log");

        // Lose the socket
        fgr_socket_channel_stop(&g_context.context_sock);
        g_context.sock = -1;

        CONTEXT_UNLOCK(g_context.lock, "clean_up() log");
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

    if (g_context.sock < 0) {
        if (!g_context.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_context.lock = xSemaphoreCreateMutex();
        }

        if (g_context.lock) {

            CONTEXT_LOCK(g_context.lock, "fgr_log_init()");

            g_context.min_level = min_level;
            // Create connection to server
            err = fgr_socket_channel_start(server_ip, port,
                                           &g_context.sock,
                                           &g_context.context_sock);
            if (err == ESP_OK) {
                // Maintain the connection
                err = fgr_socket_channel_maintain(&g_context.context_sock,
                                                  CONFIG_FGR_LOG_HEARTBEAT_SECONDS,
                                                  socket_heartbeat_cb,
                                                  socket_reconnect_cb,
                                                  &g_context);
                if (err != ESP_OK) {
                    fgr_socket_channel_stop(&g_context.context_sock);
                    g_context.sock = -1;
                }
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_log_init()");

            if (err == ESP_OK) {
                // Set vprintf handler
                g_context.connected = true;
                esp_log_set_vprintf(tcp_log_vprintf);
                ESP_LOGI(TAG, "Logs will be forwarded to %s:%d, log level %d.",
                         server_ip, port, g_context.min_level);

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

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_set_min_level()");

        g_context.min_level = level;
        err = ESP_OK;

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_set_min_level()");

        ESP_LOGI(TAG, "Log level set to %d.", g_context.min_level);
    }

    return err;
}

// Stop logging.
int32_t fgr_log_off()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_off()");

        g_context.on_not_off = false;
        err = ESP_OK;

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_off()");

        ESP_LOGI(TAG, "Logging turned off.");
    }

    return err;
}

// Turn logging back on.
int32_t fgr_log_on()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_on()");

        g_context.on_not_off = true;
        err = ESP_OK;

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_on()");

        ESP_LOGI(TAG, "Logging turned on.");
    }

    return err;
}

// A message receive callback.
bool fgr_log_msg_receive_cb(fgr_msg_t *msg, void *param)
{
    bool handled = false;

    (void) param;

    fgr_error_t msg_error = FGR_ERROR_UNHANDLED_REQUEST;

    if (IS_MSG_REQ(msg->header.req.type)) {
        // REQUEST messages
        handled = true;
        switch (MSG_MASK(msg->header.req.type)) {
            case FGR_REQ_CNF_LOG_LEVEL:
                //  Message contents should be a uint8_t that is fgr_log_level_t
                msg_error = FGR_ERROR_INVALID_PARAM;
                if (msg->body.length == 1) {
                    msg_error = FGR_ERROR_GENERIC;
                    fgr_log_level_t level = msg->body.contents[0];
                    if (fgr_log_set_min_level(level) == ESP_OK) {
                        msg_error = FGR_ERROR_NONE;
                    }
                }
            break;
            case FGR_REQ_CNF_LOG_START:
                msg_error = FGR_ERROR_GENERIC;
               if (fgr_log_on() == ESP_OK) {
                    msg_error = FGR_ERROR_NONE;
                }
            break;
            case FGR_REQ_CNF_LOG_STOP:
                msg_error = FGR_ERROR_GENERIC;
                if (fgr_log_off() == ESP_OK) {
                    msg_error = FGR_ERROR_NONE;
                }
            break;
            default:
                handled = false;
            break;
        }

        if (handled) {
            fgr_msg_send_queue_cnf(MSG_MASK(msg->header.req.type), msg_error,
                                   msg->header.req.reference, NULL, 0);
        }
    }

    if (handled) {
        // This will be printed before the queued CNF message is sent
        fgr_msg_print_summary("Handled", FGR_LOG_LEVEL_INFO, msg->header.req.type, 0,
                              msg->header.req.reference, msg->body.length);
    }

    return handled;
}

// End of file

