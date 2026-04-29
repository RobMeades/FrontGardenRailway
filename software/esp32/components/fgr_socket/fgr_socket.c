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
 * @brief Sockets helper functions for a node of the front garden railway.
 */

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "errno.h"
#include "lwip/sockets.h"

#include "fgr_socket.h"
#include "fgr_util.h"
#include "fgr_debug.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "socket"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Context for receive task.
typedef struct {
    int32_t sock;
    fgr_socket_rx_cb_t cb;
    void *cb_param;
    TaskHandle_t task_handle;
    bool running;
    volatile atomic_bool *connected;
} context_rx_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Receive context.
static context_rx_t g_context_rx = {0};

// Server structure for socket.
static struct sockaddr_in g_server = {0};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Stop any existing rx operation.
static void stop_rx(context_rx_t *context)
{
    if (context && context->running) {
        context->running = false;
        // Wait for existing rx task to exit
        vTaskDelay(pdMS_TO_TICKS(100));
        memset(context, 0, sizeof(*context));
    }
}

// Task to receive FGR protocol messages from a server.
static void task_rx(void *param)
{
    context_rx_t *context = (context_rx_t *) param;
    fgr_msg_t msg = {0};
    size_t nothing_received_count = 0;

    // Allow us to feed the watchdog
    esp_task_wdt_add(NULL);

    // Main command processing loop
    while (context->running) {

        // Use select() to check socket state before recv()
        // this reduces the chances of us getting stuck
        // if the far end doesn't close a socket nicely
        fd_set readfds;
        FD_ZERO(&readfds);
        FD_SET(context->sock, &readfds);

        struct timeval tv;
        tv.tv_sec = 0;
        tv.tv_usec = 100000; // 100 ms select timeout
        int select_ret = select(context->sock + 1, &readfds, NULL, NULL, &tv);
        if (select_ret < 0) {
            // select error - connection likely dead
            ESP_LOGE(TAG, "select() failed: %d (%s)!", errno, strerror(errno));
            context->connected = false;
        } else if (select_ret == 0) {
            // No data available, still connected
        } else {
            // Non-blocking receive
            int32_t err = recv(context->sock, &msg, sizeof(msg), 0);
            if (err > 0) {
                // Process received data
                ESP_LOGD(TAG, "Received %d byte(s) from server:", err);
                char debug_buffer[128];
                fgr_debug_hex_dump_to_buffer((const void *) &msg, err, debug_buffer, sizeof(debug_buffer));
                ESP_LOGD(TAG, "%s", debug_buffer);
                // TODO: decode and call callback when a message is received
            } else if (err == 0) {
                // Connection closed by peer
                ESP_LOGI(TAG, "Connection closed by peer!");
                context->connected = false;
            } else {
                // Error or would block
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    // No data available right now - this is expected in non-blocking mode
                    // Just yield to allow other tasks to run
                    nothing_received_count++;
                    vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
                } else {
                    // Real error occurred
                    ESP_LOGE(TAG, "recv() failed %d (%s)!", errno, strerror(errno));
                    context->connected = false;
                }
            }
        }

        esp_task_wdt_reset();

        if (!context->connected) {
            // Wait for a reconnection
            vTaskDelay(pdMS_TO_TICKS(2000));
        } else {
            // Just a short delay
            vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
        }
        if (nothing_received_count > 1000) {
            ESP_LOGI(TAG, "Waiting for a command.");
            nothing_received_count = 0;
        }
    }

    esp_task_wdt_delete(NULL);
    vTaskDelete(NULL);
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Create a socket.
int32_t fgr_socket_create(int *sock)
{
    esp_err_t err = ESP_ERR_INVALID_ARG;

    if (sock) {
        err = ESP_FAIL;
        *sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (*sock >= 0) {
            err = ESP_OK;
        } else {
            ESP_LOGE(TAG, "Unable to create socket %d (%s)!", errno, strerror(errno));
        }
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Destroy a socket.
void fgr_socket_destroy(int sock)
{
    close(sock);
}

// Set a socket to non-blocking mode.
int32_t fgr_socket_set_non_blocking(int sock, int32_t timeout_seconds)
{
    esp_err_t err = ESP_OK;
    int flags;

    if (sock < 0) {
        ESP_LOGE(TAG, "Invalid socket descriptor");
        err = ESP_FAIL;
    }

    if (err == ESP_OK) {
        // Get current flags
        flags = fcntl(sock, F_GETFL, 0);
        if (flags < 0) {
            ESP_LOGE(TAG, "fcntl F_GETFL failed: %d (%s)!", errno, strerror(errno));
            err = ESP_FAIL;
        }
    }

    if (err == ESP_OK) {
        // Set the non-blocking flag
        if (fcntl(sock, F_SETFL, flags | O_NONBLOCK) < 0) {
            ESP_LOGE(TAG, "fcntl F_SETFL (non-blocking) failed: %d (%s)!", errno, strerror(errno));
            err = ESP_FAIL;
        }
    }

    if ((err == ESP_OK) && (timeout_seconds > 0)) {
        // Also set send and receive timeouts, otherwise it is
        // still possible to get stuck in a socket if the far
        // end does not clear a socket tidily (e.g. Raspberry Pi reboot)
        struct timeval tv;
        tv.tv_sec = timeout_seconds;
        tv.tv_usec = 0;
        if (setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) != 0) {
            ESP_LOGE(TAG, "Failed to set SO_RCVTIMEO: %d (%s)!", errno, strerror(errno));
            err = ESP_FAIL;
        }

        if (err == ESP_OK) {
            // Set send timeout
            if (setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)) != 0) {
                ESP_LOGE(TAG, "Failed to set SO_SNDTIMEO: %d (%s)!", errno, strerror(errno));
                err = ESP_FAIL;
            }
        }

        if (err == ESP_OK) {
            ESP_LOGD(TAG, "Socket %d set to have %d second Rx and Tx timeouts.",
                     sock, timeout_seconds);
        }
    }

    if (err == ESP_OK) {
        ESP_LOGD(TAG, "Socket %d set to non-blocking mode.", sock);
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Enable TCP keep-alive: this allows us to detect a failure
// of Wi-Fi or of our controlling entity and fall back to
// asking for a reconnection.
int32_t fgr_socket_enable_tcp_keep_alive(int sock,
                                         int32_t tcp_keep_alive_idle_time_seconds,
                                         int32_t tcp_keep_alive_probe_interval_seconds,
                                         size_t tcp_keep_alive_count)
{
    esp_err_t err = ESP_OK;

    // Enable keep-alive
    int x = 1;
    if (setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &x, sizeof(x)) != 0) {
        ESP_LOGE(TAG, "Failed to set SO_KEEPALIVE: %d (%s)!", errno, strerror(errno));
        err = ESP_FAIL;
    }

    if (err == ESP_OK) {
        // Set idle time before keep-alive kicks in
        x = tcp_keep_alive_idle_time_seconds;
        if (setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE, &x, sizeof(x)) != 0) {
            ESP_LOGE(TAG, "Failed to set TCP_KEEPIDLE: %d (%s)!", errno, strerror(errno));
            err = ESP_FAIL;
        }
    }

    if (err == ESP_OK) {
        // Set keep-alive interval
        x = tcp_keep_alive_probe_interval_seconds;
        if (setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &x, sizeof(x)) != 0) {
            ESP_LOGE(TAG, "Failed to set TCP_KEEPINTVL: %d (%s)!", errno, strerror(errno));
            err = ESP_FAIL;
        }
    }

    if (err == ESP_OK) {
        x = tcp_keep_alive_count;
        // Set keep-alive count
        if (setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT, &x, sizeof(x)) != 0) {
            ESP_LOGE(TAG, "Failed to set TCP_KEEPCNT: %d (%s)!", errno, strerror(errno));
            err = ESP_FAIL;
        }
    }

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "TCP keep-alive enabled: detection time ~%d seconds.",
                 tcp_keep_alive_idle_time_seconds +
                 (tcp_keep_alive_probe_interval_seconds * tcp_keep_alive_count));
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Connect a socket.
int32_t fgr_socket_connect(int sock, const char *server_ip,
                           uint16_t port)
{
      // Configure server address
      g_server.sin_family = AF_INET;
      g_server.sin_port = htons(port);
      inet_pton(AF_INET, server_ip, &g_server.sin_addr);

      ESP_LOGI(TAG, "Connecting to %s:%d...",  server_ip, port);

      // Connect to the server
      esp_err_t err = connect(sock, (struct sockaddr *) &g_server, sizeof(g_server));
      if (err != 0) {
          err = ESP_FAIL;
          ESP_LOGE(TAG, "Failed to connect to server %d (%s)!", errno, strerror(errno));
      }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Create, connect and configure a socket using the default configuration values.
int32_t fgr_socket_create_connect_configure(const char *server_ip, uint16_t port,
                                            int *sock)
{
    int32_t err = fgr_socket_create(sock);

    if (err == ESP_OK) {
        err = fgr_socket_connect(*sock, server_ip, port);
    }

    if (err == ESP_OK) {
        // Put socket into non-blocking mode as we don't want to
        // get stuck in a recv()
        err = fgr_socket_set_non_blocking(*sock, FGR_SOCKET_TIMEOUT_SECONDS);
    }

    if (err == ESP_OK) {
        // Set a keep-alive so that we realise when the
        // connection has dropped relatively quickly
        err = fgr_socket_enable_tcp_keep_alive(*sock,
                                               FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS,
                                               FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS,
                                               FGR_SOCKET_TCP_KEEP_ALIVE_COUNT);
    }

    if ((err != ESP_OK) && sock && (*sock >= 0)) {
        // Tidy up on error
        fgr_socket_destroy(*sock);
    }

    return err;
}

// Send data on a socket.
int32_t fgr_socket_send(int sock, uint8_t *buffer, size_t length,
                        size_t retry_count)
{
    esp_err_t err = ESP_OK;
    size_t total_written = 0;
    size_t retries = 0;

    if (buffer) {
      while ((total_written < length) && (err == ESP_OK) && (retries < retry_count)) {
          int32_t len_written = send(sock, buffer + total_written, length - total_written, 0);
          if (len_written >= 0) {
              total_written += len_written;
              retries = 0;  // Reset on success
          } else {
              if (errno == EAGAIN || errno == EWOULDBLOCK) {
                  retries++;
                  vTaskDelay(pdMS_TO_TICKS(10));  // Small delay before retry
              } else {
                  err = -errno;
                  ESP_LOGE(TAG, "Error sending to server %d (%s)!", errno, strerror(errno));
              }
          }
      }
    }

    if (retries >= retry_count) {
        ESP_LOGE(TAG, "Send timeout after %d retries.", retries);
        err = ESP_ERR_TIMEOUT;
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Start receiving protocol messages on a socket.
int32_t fgr_socket_receive_start(int sock, volatile atomic_bool *connected,
                                 fgr_socket_rx_cb_t rx_cb,
                                 void *rx_cb_param)
{
    esp_err_t err = ESP_ERR_NO_MEM;
    context_rx_t *context = &g_context_rx;

    // Stop any existing rx operation
    stop_rx(context);

    // Start an rx operation
    context->sock = sock;
    context->connected = connected;
    context->cb = rx_cb;
    context->cb_param = rx_cb_param;
    context->running = true;
    if (xTaskCreate(&task_rx, "socket_rx", 1024 * 4, context, 5, &context->task_handle) == pdPASS) {
        err = ESP_OK;
    } else {
        memset(context, 0, sizeof(*context));
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Stop receiving FGR protocol messages on a socket.
void fgr_socket_receive_stop()
{
    stop_rx(&g_context_rx);
}

// End of file

