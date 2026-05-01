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
 * @brief Implementation of the messaging interface for a node of the
 * front garden railway.
 */

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_log.h"

#include "fgr_util.h"
#include "fgr_socket.h"
#include "fgr_msg.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix.
 #define TAG "msg"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Context.
typedef struct {
    int sock;
    void *context_sock;
    bool connected;
    SemaphoreHandle_t lock;
} msg_cfg_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static msg_cfg_t g_msg_cfg = {
    .sock = -1
};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Callback called by fgr_socket_channel_maintain().
static void socket_reconnect_cb(int sock, void *param)
{
    msg_cfg_t *msg_cfg = (msg_cfg_t *) param;

    if (msg_cfg->lock) {

        CONTEXT_LOCK(msg_cfg->lock, "socket_reconnect_cb() 2");
        int32_t err = fgr_socket_enable_tcp_keep_alive(sock,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_COUNT);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "fgr_socket_enable_tcp_keep_alive() returned error: %s.", esp_err_to_name(err));
        }
        msg_cfg->sock = sock;
        msg_cfg->connected = true;
        CONTEXT_UNLOCK(msg_cfg->lock, "socket_reconnect_cb() 2");
    }
}

// Clean up.
static void clean_up()
{
    if (g_msg_cfg.lock) {

        CONTEXT_LOCK(g_msg_cfg.lock, "clean_up() 2");

        // Lose the socket
        fgr_socket_channel_stop(&g_msg_cfg.context_sock);
        g_msg_cfg.sock = -1;

        CONTEXT_UNLOCK(g_msg_cfg.lock, "clean_up() 2");
        // Don't delete the semaphore, someone might have it still
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise the messaging interface.
int32_t fgr_msg_init(const char *server_ip, uint16_t port)
{
    int32_t err = ESP_OK;

    if (g_msg_cfg.sock < 0) {
        if (!g_msg_cfg.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_msg_cfg.lock = xSemaphoreCreateMutex();
        }

        if (g_msg_cfg.lock) {

            CONTEXT_LOCK(g_msg_cfg.lock, "fgr_msg_init()");

            // Create connection to server
            err = fgr_socket_channel_start(server_ip, port,
                                           &g_msg_cfg.sock,
                                           &g_msg_cfg.context_sock);
            if (err == ESP_OK) {

                CONTEXT_UNLOCK(g_msg_cfg.lock, "fgr_msg_init()");
                // Do initial extra socket configuration
                socket_reconnect_cb(g_msg_cfg.sock, &g_msg_cfg);
                CONTEXT_LOCK(g_msg_cfg.lock, "fgr_msg_init()");

                // Maintain the connection
                err = fgr_socket_channel_maintain(&g_msg_cfg.context_sock,
                                                  socket_reconnect_cb,
                                                  &g_msg_cfg);
                if (err != ESP_OK) {
                    fgr_socket_channel_stop(&g_msg_cfg.context_sock);
                    g_msg_cfg.sock = -1;
                }
            }

            CONTEXT_UNLOCK(g_msg_cfg.lock, "fgr_msg_init()");
        }
    }

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Connected to controller.");
    } else {
        clean_up();
    }

    return (int32_t) err;
}

// Deinitialise the messaging interface.
void fgr_msg_deinit()
{
    clean_up();
}

// End of file

