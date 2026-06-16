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

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "errno.h"
#include "lwip/sockets.h"

#include "fgr_util.h"
#include "fgr_monitor.h"
#include "fgr_task.h"
#include "fgr_debug.h"
#include "fgr_socket.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "socket"

#ifndef FGR_SOCKET_RX_BUFFER_LENGTH
// Receive buffer length: just
#  define FGR_SOCKET_RX_BUFFER_LENGTH 512
#endif

#ifndef FGR_SOCKET_RECONNECT_TIMEOUT_MS
// Reconnection timeout in milliseconds
#  define FGR_SOCKET_RECONNECT_TIMEOUT_MS  5000
#endif

#ifndef FGR_SOCKET_MAINTAIN_WAIT_MS
// Reconnection wait time in milliseconds
#  define FGR_SOCKET_MAINTAIN_WAIT_MS  1000
#endif

#ifndef FGR_SOCKET_TASK_RX_STACK_SIZE
#  define FGR_SOCKET_TASK_RX_STACK_SIZE (1024 * 6)
#endif

#ifndef FGR_SOCKET_TASK_MAINTAIN_STACK_SIZE
#  define FGR_SOCKET_TASK_MAINTAIN_STACK_SIZE (1024 * 4)
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Context for a non-blocking connect.
typedef struct {
    int sock;
    struct sockaddr_in server;
    bool connected;
} context_connect_t;

// Context for a channel (a connection that may be maintained).
typedef struct {
    int sock;
    const char *server_ip;
    uint16_t port;
    bool connected;
    SemaphoreHandle_t lock;
    int64_t last_activity_time_us;
    size_t heartbeat_seconds;
    fgr_socket_channel_cb_t heartbeat_cb;
    fgr_socket_channel_down_cb_t down_cb;
    fgr_socket_channel_cb_t cfg_cb;
    void *cb_param;
    TaskHandle_t task_handle;
} context_channel_t;

// Context for receive task.
typedef struct {
    int sock;
    fgr_socket_rx_cb_t rx_cb;
    void *rx_cb_param;
    fgr_socket_reconnect_cb_t reconnect_cb;
    void **reconnect_cb_param;
    TaskHandle_t task_handle;
} context_rx_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLNACKS
 * -------------------------------------------------------------- */

// Task callback to maintain a socket.
// This is _extremely_ complex because the socket is non-blocking
// and because the reconnect process can take a while, causing the
// task watchdog to go off.
static void task_maintain_cb(void *handle, void *param)
{
    context_channel_t *context_channel = (context_channel_t *) param;
    void *context_connect = NULL;
    bool down_cb_called = false;

    // Try to take lock with timeout
    if (xSemaphoreTake(context_channel->lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        if (context_channel->connected) {
            // Connected: send heartbeat if necessary
            if ((context_channel->heartbeat_seconds > 0) &&
                (context_channel->heartbeat_cb != NULL) &&
                (esp_timer_get_time() - context_channel->last_activity_time_us > ((int64_t) context_channel->heartbeat_seconds) * 1000000)) {
                // Unlock the channel context here so that heartbeat_cb()
                // can call fgr_socket_channel_activity() or fgr_socket_channel_failed()
                xSemaphoreGive(context_channel->lock);
                context_channel->heartbeat_cb(context_channel->sock, context_channel->cb_param);
            } else {
                xSemaphoreGive(context_channel->lock);
            }
        } else {
            ESP_LOGW(TAG, "%s:%d connection lost, reconnecting...",
                     context_channel->server_ip, context_channel->port);

            // Close old socket if it exists
            fgr_socket_destroy(&context_channel->sock);

            // Create new socket into a thread-local variable
            int local_sock = -1;
            int32_t err = fgr_socket_create(&local_sock);
            if (err == ESP_OK) {
                // Make socket non-blocking
                err = fgr_socket_set_non_blocking(local_sock, 0);
                if (err == ESP_OK) {
                    // Initiate non-blocking connect using local variable
                    err = fgr_socket_connect_start(local_sock,
                                                   context_channel->server_ip,
                                                   context_channel->port,
                                                   &context_connect);

                    xSemaphoreGive(context_channel->lock);

                    if (err == ESP_OK) {
                        // Connected immediately

                        CONTEXT_LOCK(context_channel->lock, "task_maintain_cb() 1");
                        context_channel->sock = local_sock; // Safely publish to context
                        context_channel->connected = true;
                        context_channel->last_activity_time_us = esp_timer_get_time();
                        CONTEXT_UNLOCK(context_channel->lock, "task_maintain_cb() 1");

                        ESP_LOGI(TAG, "Reconnected immediately.");
                    } else if (err == -ESP_ERR_NOT_FINISHED) {
                        // Connection in progress
                        int32_t elapsed_ms = 0;
                        while ((err == -ESP_ERR_NOT_FINISHED) && (elapsed_ms < FGR_SOCKET_RECONNECT_TIMEOUT_MS) &&
                               context_channel->task_handle) {
                            int32_t timeout_ms = 100;
                            err = fgr_socket_connect_is_complete(timeout_ms, &context_connect);
                            elapsed_ms += timeout_ms;
                            vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
                            fgr_monitor_task_wdt_feed(handle);
                        }

                        CONTEXT_LOCK(context_channel->lock, "task_maintain_cb() 2");
                        if (err == ESP_OK) {
                            context_channel->sock = local_sock; // Safely publish to context
                            context_channel->connected = true;
                            context_channel->last_activity_time_us = esp_timer_get_time();
                            ESP_LOGI(TAG, "Reconnected after %" PRId32 " ms.", elapsed_ms);
                        } else {
                            if (context_connect != NULL) {
                                fgr_socket_connect_stop(&context_connect);
                            }
                            if (local_sock >= 0) {
                                shutdown(local_sock, SHUT_RDWR);  // To unblock any LWIP callbacks
                                close(local_sock);
                            }
                        }
                        CONTEXT_UNLOCK(context_channel->lock, "task_maintain_cb() 2");

                    } else {
                        // Connect failed immediately

                        ESP_LOGE(TAG, "Connect failed immediately: %d (%s)", errno, strerror(errno));
                        CONTEXT_LOCK(context_channel->lock, "task_maintain_cb() 3");
                        if (context_connect != NULL) {
                            fgr_socket_connect_stop(&context_connect);
                        }
                        if (local_sock >= 0) {
                            shutdown(local_sock, SHUT_RDWR);  // To unblock any LWIP callbacks
                            close(local_sock);
                        }
                        CONTEXT_UNLOCK(context_channel->lock, "task_maintain_cb() 3");
                    }
                } else {
                    xSemaphoreGive(context_channel->lock);
                    if (local_sock >= 0) {
                        shutdown(local_sock, SHUT_RDWR);  // To unblock any LWIP callbacks
                        close(local_sock);
                    }
                }
            } else {
                xSemaphoreGive(context_channel->lock);
            }

            if (context_channel->connected) {
                down_cb_called = false;
                if (context_channel->cfg_cb) {
                    // If we have reconnected and there is a user configuration
                    // callback, call it
                    context_channel->cfg_cb(context_channel->sock,
                                            context_channel->cb_param);
                    if (context_channel->heartbeat_cb) {
                        // Call heartbeat_cb() also so that the controller gets our state
                        context_channel->heartbeat_cb(context_channel->sock,
                                                      context_channel->cb_param);
                    }
                }
            } else {
                if (context_channel->down_cb && !down_cb_called) {
                    // Call down_cb() so that the application knows we're having trouble
                    context_channel->down_cb(context_channel->cb_param);
                    down_cb_called = true;
                }
                // Wait a little before trying again
                vTaskDelay(pdMS_TO_TICKS(FGR_SOCKET_MAINTAIN_WAIT_MS));
            }
        }
    } else {
        ESP_LOGW(TAG, "Could not take lock, skipping channel maintenance this time.");
    }
}

// Callback task to receive data from a server.
static void task_rx_cb(void *handle, void *param)
{
    context_rx_t *context = (context_rx_t *) param;
    uint8_t buffer[FGR_SOCKET_RX_BUFFER_LENGTH];
    size_t nothing_received_count = 0;
    bool connected;
    bool awaiting_reconnect = false;

    // Use select() to check socket state before recv()
    // this reduces the chances of us getting stuck
    // if the far end doesn't close a socket nicely
    fd_set readfds;
    FD_ZERO(&readfds);
    FD_SET(context->sock, &readfds);

    connected = true;
    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 100000; // 100 ms select timeout
    int select_ret = select(context->sock + 1, &readfds, NULL, NULL, &tv);
    if (select_ret < 0) {
        // select error - connection likely dead
        ESP_LOGE(TAG, "select() failed: %d (%s)!", errno, strerror(errno));
        connected = false;
    } else if (select_ret == 0) {
        // No data available, still connected
    } else {
        // Non-blocking receive
        int32_t err = recv(context->sock, buffer, sizeof(buffer), 0);
        if (err > 0) {
            // Process received data
            ESP_LOGD(TAG, "Received %d byte(s) from server:", err);
            char buffer_str[128];
            fgr_debug_hex_dump_to_buffer((const void *) buffer, err, buffer_str, sizeof(buffer_str));
            ESP_LOGD(TAG, "%s", buffer_str);
            if (context->rx_cb) {
                context->rx_cb(buffer, err, context->rx_cb_param);
            }
        } else if (err == 0) {
            // Connection closed by peer
            ESP_LOGE(TAG, "Connection closed by peer!");
            connected = false;
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
                connected = false;
            }
        }
    }

    fgr_monitor_task_wdt_feed(handle);

    if (connected) {
        awaiting_reconnect = false;
    } else if (context->reconnect_cb && !awaiting_reconnect) {
        // Trigger a reconnection
        context->reconnect_cb(context->reconnect_cb_param);
        awaiting_reconnect = true;
    }

    if (!connected) {
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

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Send data on a socket.
static int32_t socket_send(int sock, const void *buffer, size_t length,
                           size_t retry_count, bool log)
{
    esp_err_t err = ESP_OK;
    size_t total_written = 0;
    size_t retries = 0;

    if (buffer) {
        while ((total_written < length) && (err == ESP_OK) && (retries <= retry_count)) {
            int32_t len_written = send(sock, ((uint8_t *) buffer) + total_written, length - total_written, MSG_DONTWAIT);
            if (len_written >= 0) {
                total_written += len_written;
                retries = 0;  // Reset on success
            } else {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    retries++;
                    vTaskDelay(pdMS_TO_TICKS(10));  // Small delay before retry
                } else {
                    err = -errno;
                    if (log) {
                        ESP_LOGE(TAG, "Error sending to server %d (%s)!", errno, strerror(errno));
                    }
                }
            }
        }
    }

    if (retries > retry_count) {
        if (log) {
            ESP_LOGE(TAG, "Send timeout after %d retries.", retries);
        }
        err = ESP_ERR_TIMEOUT;
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) - err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: SIMPLE OPERATIONS
 * -------------------------------------------------------------- */

// Create a socket.
int32_t fgr_socket_create(int *sock)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (sock) {
        err = ESP_FAIL;
        *sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (*sock >= 0) {
            err = ESP_OK;
        } else {
            ESP_LOGE(TAG, "Unable to create socket %d (%s)!", errno, strerror(errno));
        }
    }

    return err;
}

// Destroy a socket.
void fgr_socket_destroy(int *sock)
{
    if (sock && (*sock >= 0)) {
        // Call shutdown() to unblock any LWIP callbacks
        int32_t err = shutdown(*sock, SHUT_RDWR);
        if (err != 0) {
            // ENOTCONN is harmless - socket wasn't connected
            if (errno != ENOTCONN) {
                ESP_LOGW(TAG, "shutdown() failed for socket %d: %d (%s)",
                         *sock, errno, strerror(errno));
            }
        }
        close(*sock);
        *sock = -1;
    }
}

// Set a socket to non-blocking mode.
int32_t fgr_socket_set_non_blocking(int sock, int32_t timeout_seconds)
{
    int32_t err = ESP_OK;
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

    return err;
}

// Enable TCP keep-alive: this allows us to detect a failure
// of Wi-Fi or of our controlling entity and fall back to
// asking for a reconnection.
int32_t fgr_socket_enable_tcp_keep_alive(int sock,
                                         int32_t tcp_keep_alive_idle_time_seconds,
                                         int32_t tcp_keep_alive_probe_interval_seconds,
                                         size_t tcp_keep_alive_count)
{
    int32_t err = ESP_OK;

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

    return err;
}

// Disable Nagle's algorithm on a log socket
int32_t fgr_socket_enable_tcp_no_delay(int sock)
{
    int32_t err = ESP_OK;

    // Enable keep-alive
    int x = 1;
    if (setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &x, sizeof(x)) != 0) {
        ESP_LOGE(TAG, "Failed to set TCP_NODELAY: %d (%s)!", errno, strerror(errno));
        err = ESP_FAIL;
    }

    return err;
}

// Connect a socket, blocking version
int32_t fgr_socket_connect(int sock, const char *server_ip, uint16_t port)
{
    struct sockaddr_in server = {0};
    // Configure server address
    server.sin_family = AF_INET;
    server.sin_port = htons(port);
    inet_pton(AF_INET, server_ip, &server.sin_addr);

    ESP_LOGI(TAG, "Connecting to %s:%d...",  server_ip, port);

    // Connect to the server
    int32_t err = connect(sock, (struct sockaddr *) &server, sizeof(server));
    if (err != 0) {
        err = ESP_FAIL;
        ESP_LOGE(TAG, "Failed to connect to server %d (%s)!", errno, strerror(errno));
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: NON-BLOCKING CONNECT
 * -------------------------------------------------------------- */

// Start to connect a socket (i.e. non-blocking).
int32_t fgr_socket_connect_start(int sock, const char *server_ip,
                                 uint16_t port, void **context)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (server_ip && context) {
        err = -ESP_ERR_NO_MEM;
        *context = malloc(sizeof(context_connect_t));
        context_connect_t *context_connect = (context_connect_t *) *context;
        if (context_connect) {
            memset(context_connect, 0, sizeof(*context_connect));
            context_connect->sock = sock;
            // Configure server address
            context_connect->server.sin_family = AF_INET;
            context_connect->server.sin_port = htons(port);
            inet_pton(AF_INET, server_ip, &context_connect->server.sin_addr);

            ESP_LOGI(TAG, "Start connecting to %s:%d...",  server_ip, port);

            // Start connecting to the server
            err = ESP_FAIL;
            if (connect(sock, (struct sockaddr *) &context_connect->server,
                        sizeof(context_connect->server)) == 0) {
                // Connected immediately
                err = ESP_OK;
            } else if (errno == EINPROGRESS) {
                // Need to wait for connection
                err = -ESP_ERR_NOT_FINISHED;
            } else {
                ESP_LOGE(TAG, "Connect failed immediately: %d (%s)", errno, strerror(errno));
            }

            if (err != -ESP_ERR_NOT_FINISHED) {
                // Don't need the context if we're connected or failed immediately
                free(*context);
                *context = NULL;
            }
        }
    }

    return err;
}

// Check the progress of a non-blocking connection
int32_t fgr_socket_connect_is_complete(int32_t timeout_ms, void **context)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (fgr_util_is_valid_ptr_to_ptr(context, TAG, "socket context", __FILE__, __LINE__) &&
        context && *context) {

        err = -ESP_ERR_NOT_FINISHED;
        context_connect_t *context_connect = (context_connect_t *) *context;
        fd_set fdset;
        struct timeval tv;

        FD_ZERO(&fdset);
        FD_SET(context_connect->sock, &fdset);
        tv.tv_sec = 0;
        tv.tv_usec = timeout_ms * 1000;

        int32_t sel_rc = select(context_connect->sock + 1, NULL, &fdset, NULL, &tv);
        if (sel_rc > 0) {
            int32_t so_error;
            socklen_t len = sizeof(so_error);
            getsockopt(context_connect->sock, SOL_SOCKET, SO_ERROR, &so_error, &len);
            if (so_error == 0) {
                // Verify with getpeername() to make completely sure the socket has reconnected
                struct sockaddr_in peer;
                socklen_t peer_len = sizeof(peer);
                if (getpeername(context_connect->sock, (struct sockaddr *)&peer, &peer_len) == 0) {
                    // Truly connected
                    err = ESP_OK;
                } else if (errno == ENOTCONN) {
                    // Still connecting
                    err = -ESP_ERR_NOT_FINISHED;
                    ESP_LOGD(TAG, "Socket not connected yet (ENOTCONN)");
                } else {
                    // Other error
                    err = ESP_FAIL;
                    ESP_LOGE(TAG, "getpeername() failed: %d (%s)", errno, strerror(errno));
                }
            } else {
                errno = so_error;  // Set errno to the socket error as getsockopt() doesn't set it
                err = ESP_FAIL;
                ESP_LOGD(TAG, "Connect failed: %d (%s)", (int) so_error, strerror(so_error));
            }
        } else if (sel_rc < 0) {
            err = ESP_FAIL;
            ESP_LOGE(TAG, "Select error: %d (%s)", errno, strerror(errno));
        }

        if (err != -ESP_ERR_NOT_FINISHED) {
            // Don't need the context if we're connected or failed
            free(*context);
            *context = NULL;
        }
    }

    return err;
}

// Stop connecting a non-blocking socket.
void fgr_socket_connect_stop(void **context)
{
    if (fgr_util_is_valid_ptr_to_ptr(context, TAG, "socket context", __FILE__, __LINE__) && context) {
        free(*context);
        *context = NULL;
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: "CHANNEL" COMPOUND OPERATIONS
 * -------------------------------------------------------------- */

// Create and connect a [non-blocking] socket.
int32_t fgr_socket_channel_start(const char *server_ip, uint16_t port,
                                 int *sock, void **context)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (server_ip && sock && context) {
        *context = NULL;
        *sock = -1;
        err = fgr_socket_create(sock);
        if (err == ESP_OK) {
            err = fgr_socket_connect(*sock, server_ip, port);
        }
        if (err == ESP_OK) {
            // Put socket into non-blocking mode as we don't want to
            // get stuck in a recv()
            err = fgr_socket_set_non_blocking(*sock, FGR_SOCKET_TIMEOUT_SECONDS);
        }
        if (err == ESP_OK) {
            err = -ESP_ERR_NO_MEM;
            *context = malloc(sizeof(context_channel_t));
            context_channel_t *context_channel = (context_channel_t *) *context;
            if (context_channel) {
                memset(context_channel, 0, sizeof(*context_channel));
                // Create mutex
                context_channel->lock = xSemaphoreCreateMutex();
                if (context_channel->lock) {
                    err = ESP_OK;
                    CONTEXT_LOCK(context_channel->lock, "fgr_socket_channel_start()");
                    context_channel->sock = *sock;
                    context_channel->server_ip = server_ip;
                    context_channel->port = port;
                    context_channel->connected = true;
                    context_channel->last_activity_time_us = esp_timer_get_time();
                    CONTEXT_UNLOCK(context_channel->lock, "fgr_socket_channel_start()");
                }
            }
        }
        if (err != ESP_OK) {
            // Tidy up on error
            free(*context);
            *context = NULL;
            if (*sock >= 0) {
                fgr_socket_destroy(sock);
            }
        }
    }

    return err;
}

// Maintain a socket connection.
int32_t fgr_socket_channel_maintain(void **context,
                                    size_t heartbeat_seconds,
                                    fgr_socket_channel_cb_t heartbeat_cb,
                                    fgr_socket_channel_cb_t cfg_cb,
                                    fgr_socket_channel_down_cb_t down_cb,
                                    void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (fgr_util_is_valid_ptr_to_ptr(context, TAG, "channel context", __FILE__, __LINE__) &&
        context && *context && ((heartbeat_seconds == 0) || (heartbeat_cb != NULL))) {

        context_channel_t *context_channel = (context_channel_t *) *context;
        // Start a task that will send hearbeats and reconnect the socket
        // in the background on failure

        CONTEXT_LOCK(context_channel->lock, "fgr_socket_maintain()");
        context_channel->heartbeat_seconds = heartbeat_seconds;
        context_channel->heartbeat_cb = heartbeat_cb;
        context_channel->down_cb = down_cb;
        context_channel->cfg_cb = cfg_cb;
        context_channel->cb_param = cb_param;
        err = fgr_task_create(&task_maintain_cb, context_channel, "socket_maintain",
                              FGR_SOCKET_TASK_MAINTAIN_STACK_SIZE,
                              5, &context_channel->task_handle);
        if (err == ESP_OK) {
            char buffer[32];
            snprintf(buffer, sizeof(buffer), "heartbeat %d second(s)", context_channel->heartbeat_seconds);
            ESP_LOGI(TAG, "Maintaining connection to %s:%d, %s.",
                     context_channel->server_ip, context_channel->port,
                     context_channel->heartbeat_seconds ? buffer : "no hearbeat though");
        } else {
            context_channel->heartbeat_seconds = 0;
            context_channel->heartbeat_cb = NULL;
            context_channel->down_cb = NULL;
            context_channel->cfg_cb = NULL;
            context_channel->cb_param = NULL;
            ESP_LOGE(TAG, "Failed to create reconnect task %d (%s)!", errno, strerror(errno));
        }
        CONTEXT_UNLOCK(context_channel->lock, "fgr_socket_maintain()");
    }

    return err;
}

// Log activity on a channel.
void fgr_socket_channel_activity(void **context)
{
    if (fgr_util_is_valid_ptr_to_ptr(context, TAG, "channel context", __FILE__, __LINE__) &&
        context && *context) {

        context_channel_t *context_channel = (context_channel_t *) *context;
        if (context_channel->lock) {

            CONTEXT_LOCK(context_channel->lock, "fgr_socket_channel_activity()");
            context_channel->last_activity_time_us = esp_timer_get_time();
            CONTEXT_UNLOCK(context_channel->lock, "fgr_socket_channel_activity()");
        }
    }
}

// Trigger a reconnection attempt.
void fgr_socket_channel_failed(void **context)
{
    if (fgr_util_is_valid_ptr_to_ptr(context, TAG, "channel context", __FILE__, __LINE__) &&
        context && *context) {

        context_channel_t *context_channel = (context_channel_t *) *context;
        if (context_channel->lock) {

            CONTEXT_LOCK(context_channel->lock, "fgr_socket_channel_failed()");
            context_channel->connected = false;
            CONTEXT_UNLOCK(context_channel->lock, "fgr_socket_channel_failed()");
        }
    }
}

// Stop a socket connection that was started by fgr_socket_start().
void fgr_socket_channel_stop(void **context)
{
    if (fgr_util_is_valid_ptr_to_ptr(context, TAG, "channel context", __FILE__, __LINE__) &&
        context && *context) {

        context_channel_t *context_channel = (context_channel_t *) *context;

        if (context_channel->lock) {

            // Need to do this before taking the lock or we
            // will lock-up the task exit
            fgr_task_destroy(context_channel->task_handle);
            context_channel->task_handle = NULL;

            CONTEXT_LOCK(context_channel->lock, "fgr_socket_stop()");

            // Lose the socket
            fgr_socket_destroy(&context_channel->sock);

            // Just in case
            context_channel->heartbeat_cb = NULL;
            context_channel->down_cb = NULL;
            context_channel->cfg_cb = NULL;
            context_channel->cb_param = NULL;

            CONTEXT_UNLOCK(context_channel->lock, "fgr_socket_stop()");
        }

        vSemaphoreDelete(context_channel->lock);
        free(*context);
        *context = NULL;
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: SEND AND RECEIVE
 * -------------------------------------------------------------- */

// Send data on a socket.
int32_t fgr_socket_send(int sock, const void *buffer, size_t length,
                        size_t retry_count)
{
    return socket_send(sock, buffer, length, retry_count, true);
}

// Send data on a socket without any logging.
int32_t fgr_socket_send_no_log(int sock, const void *buffer, size_t length,
                               size_t retry_count)
{
    return socket_send(sock, buffer, length, retry_count, false);
}

// Start receiving data on a socket.
int32_t fgr_socket_receive_start(int sock,
                                 fgr_socket_reconnect_cb_t reconnect_cb,
                                 void **reconnect_cb_param,
                                 fgr_socket_rx_cb_t rx_cb,
                                 void *rx_cb_param, void **context)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (context) {
        err = -ESP_ERR_NO_MEM;
        *context = malloc(sizeof(context_rx_t));
        context_rx_t *context_rx = (context_rx_t *) *context;
        if (context_rx) {
            memset(context_rx, 0, sizeof(*context_rx));
            // Start an rx operation
            context_rx->sock = sock;
            context_rx->reconnect_cb = reconnect_cb;
            context_rx->reconnect_cb_param = reconnect_cb_param;
            context_rx->rx_cb = rx_cb;
            context_rx->rx_cb_param = rx_cb_param;
            err = fgr_task_create(&task_rx_cb, context_rx, "socket_rx",
                                  FGR_SOCKET_TASK_RX_STACK_SIZE,
                                  5, &context_rx->task_handle);
        }
        if ((err != ESP_OK) && context) {
            free(*context);
            *context = NULL;
        }
    }

    return err;
}

// Stop receiving data on a socket.
void fgr_socket_receive_stop(void **context)
{
    if (context) {
        context_rx_t *context_rx = (context_rx_t *) *context;
        if (context_rx && context_rx->task_handle) {

            fgr_task_destroy(context_rx->task_handle);
            context_rx->task_handle = NULL;
            free(*context);
            *context = NULL;
        }
    }
}

// End of file
