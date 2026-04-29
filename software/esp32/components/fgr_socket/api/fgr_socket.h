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

#ifndef _FGR_SOCKET_H_
#define _FGR_SOCKET_H_

/** @file
 * @brief Sockets helper functions for a node of the front garden railway.
 */

#ifdef __cplusplus
extern "C" {
#endif

#include "stdatomic.h"
#include "../../../../../protocol/fgr_protocol.h" // For fgr_log_level_t

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS
// Idle time before TCP keep-alive kicks in, in seconds.
#  define FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS   20
#endif

#ifndef FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS
// Keep alive probe interval, in seconds.
#  define FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS   10
#endif

#ifndef FGR_SOCKET_TCP_KEEP_ALIVE_COUNT
// The number of TCP probes to lose before considering the connection dead.
#  define FGR_SOCKET_TCP_KEEP_ALIVE_COUNT   3
#endif

#ifndef FGR_SOCKET_TIMEOUT_SECONDS
// Socket timeout (use 0 for none).
#  define FGR_SOCKET_TIMEOUT_SECONDS 5
#endif

#ifndef FGR_SOCKET_TX_RETRY_COUNT
// Retry count for socket sends.
#  define FGR_SOCKET_TX_RETRY_COUNT 100
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Function to call when an FGR protocol message is received.
 *
 * @param msg    a pointer to the message: this should be handled/
 *               copied/whatever before the callback returns.
 * @param param  rx_cb_param as passed to fgr_socket_receive_start().
 */
typedef void (*fgr_socket_rx_cb_t)(fgr_msg_t *msg, void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Create a socket.
 *
 * @param sock   a pointer to a place to put the socket.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_create(int *sock);

/** Destroy a socket.
 *
 * @param sock   the socket.
 */
void fgr_socket_destroy(int sock);

/** Set a socket to non-blocking mode.
 *
 * @param sock            the socket.
 * @param timeout_seconds the timeout in seconds, e.g.
 *                        FGR_SOCKET_TIMEOUT_SECONDS.
 * @return                ESP_OK on success, else a negative
 *                        value from esp_err_t.
 */
int32_t fgr_socket_set_non_blocking(int sock, int32_t timeout_seconds);

/** Enable TCP keep-alive: this allows us to detect a failure
 * of Wi-Fi or of our controlling entity and fall back to
 * asking for a reconnection.
 *
 * @param sock                              the socket.
 * @param keep_alive_idle_time_seconds      the keep alive idle time
 *                                          in seconds, e.g.
 *                                          FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS.
 * @param keep_alive_probe_interval_seconds the keep alive probe interval in
 *                                          in seconds, e.g.
 *                                          FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS.
 * @param keep_alive_count                  the keep alive count, e.g.
 *                                          FGR_SOCKET_TCP_KEEP_ALIVE_COUNT.
 * @return                                  ESP_OK on success, else a negative
 *                                          value from esp_err_t.
 */
int32_t fgr_socket_enable_tcp_keep_alive(int sock,
                                         int32_t tcp_keep_alive_idle_time_seconds,
                                         int32_t tcp_keep_alive_probe_interval_seconds,
                                         size_t tcp_keep_alive_count);

/** Connect a socket.
 *
 * @param sock        the socket to connect.
 * @param server_ip   the IP address of the server to connect to, as a
 *                    null-terminated string.
 * @param port        the port on the server to connect to.
 * @return            ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_connect(int sock, const char *server_ip, uint16_t port);

/** Create, connect and configure a socket using the default configuration
 * values: this does fgr_socket_create(), fgr_socket_connect(),
 * fgr_socket_set_non_blocking() and fgr_socket_enable_tcp_keep_alive().
 *
 * @param server_ip   the IP address of the server as a null-terminated
 *                    string.
 * @param port        the port on the server to connect to.
 * @param sock        a place to put the socket.
 * @return            ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_create_connect_configure(const char *server_ip, uint16_t port,
                                            int *sock);

/** Send data on a socket.
 *
 * @param sock           the socket.
 * @param buffer         a pointer to the data to send.
 * @param length         the amount of data in buffer.
 * @param retry_count    how many times to retry on failure, e.g.
 *                       FGR_SOCKET_TX_RETRY_COUNT.
 * @return               ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_send(int sock, uint8_t *buffer, size_t length,
                        size_t retry_count);

/** Start receiving FGR protocol messages on a socket.  Messages
 * will be received and rx_cb may be called until fgr_socket_receive_stop()
 * is called.
 *
 * @param sock          the socket.k
 * @param connected     a pointer to a Boolean indicating that the connection
 *                      is up: this may be checked by the receive task
 *                      and will be set to false by the receive task
 *                      if it detects that the connection has dropped.
 *                      This pointer must remain valid until
 *                      fgr_socket_receive_stop() is called.
 * @param rx_cb         callback to be called when a whole message
 *                      is received.
 * @param rx_cb_param   user parameter to be passed to rx_cb()
 *                      when it is called; may be NULL.
 * @return              ESP_OK on success, else a negative value
 *                      from esp_err_t.
 */
int32_t fgr_socket_receive_start(int sock, volatile atomic_bool *connected,
                                 fgr_socket_rx_cb_t rx_cb,
                                 void *rx_cb_param);

/** Stop receiving FGR protocol messages on a socket.  When this
 * function has returned rx_cb will no longer be called.
 */
void fgr_socket_receive_stop(void);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_SOCKET_H_

// End of file
