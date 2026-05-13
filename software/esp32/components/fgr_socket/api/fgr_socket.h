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

/** Function to call to configure a socket or to send a heartbeat.
 *
 * @param sock   the socket.
 * @param param  cb_param as passed to fgr_socket_channel_maintain().
 */
typedef void (*fgr_socket_channel_cb_t)(int sock, void *param);

/** Function to call to when a channel has become disconnected and
 * reconnection is taking a while.
 *
 * @param param  cb_param as passed to fgr_socket_channel_maintain().
 */
typedef void (*fgr_socket_channel_down_cb_t)(void *param);

/** Function to call when data is received on a socket.
 *
 * @param buffer a pointer to the data: this should be handled/
 *               copied/whatever before the callback returns.
 * @param length the amount of data at buffer.
 * @param param  rx_cb_param as passed to fgr_socket_receive_start().
 */
typedef void (*fgr_socket_rx_cb_t)(void *buffer, size_t length,
                                   void *param);

/** Function to call to trigger a reconnction: matches the
 * function signature of fgr_socket_channel_failed().
 *
 * @param context reconnect_cb_param as passed to
 *                fgr_socket_receive_start().
 */
typedef void (*fgr_socket_reconnect_cb_t)(void **context);

/* ----------------------------------------------------------------
 * FUNCTIONS: SIMPLE OPERATIONS
 * -------------------------------------------------------------- */

/** Create a socket.
 *
 * @param sock   a pointer to a place to put the socket.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_create(int *sock);

/** Destroy a socket.
 *
 * @param sock  a pointer to the socket; *sock will be set to -1
 *              before returning.
 */
void fgr_socket_destroy(int *sock);

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

/** Disable Nagle's algorithm on the log socket; more likely
 * to spot socket failures early this way.
 *
 * @param sock the socket.
 * @return     ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_enable_tcp_no_delay(int sock);

/** Connect a socket, blocking version, to be used on blocking sockets;
 * if you have called fgr_socket_set_non_blocking() on the socket you
 * MUST use fgr_socket_connect_start() instead.
 *
 * @param sock        the socket to connect.
 * @param server_ip   the IP address of the server to connect to, as a
 *                    null-terminated string.
 * @param port        the port on the server to connect to.
 * @return            ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_connect(int sock, const char *server_ip, uint16_t port);

/* ----------------------------------------------------------------
 * FUNCTIONS: NON-BLOCKING CONNECT
 * -------------------------------------------------------------- */

/** Start connecting to a socket, MUST be used on sockets
 * that fgr_socket_set_non_blocking() has been called on.
 *
 * If this function returns ESP_OK, you are done, you are connected,
 * you should not call fgr_socket_connect_is_complete()
 * (it will return an error) and there is no need to call
 * fgr_socket_connect_stop() (though there is no harm in
 * doing to): just use the connected socket.
 *
 * If this function returns -ESP_FAIL you're also done but you
 * are NOT connected.  There is no point in calling
 * fgr_socket_connect_is_complete() because you never will be
 * connected and there is no need need to call fgr_socket_connect_stop()
 * (though there is no harm in doing so); you should probably
 * call fgr_socket_destroy() to close the socket and start over.
 *
 * If this function returns -ESP_ERR_NOT_FINISHED then you are
 * not yet connected but asynchronous connection attempts will
 * be going on in the background.  You should call
 * fgr_socket_connect_is_complete() periodically to check for the
 * connection completing.  If the connection does not complete
 * within your desired time frame, you should call
 * fgr_socket_connect_stop(), then probably call
 * call fgr_socket_destroy() to close the socket and start over.
 *
 * @param sock        the socket to connect.
 * @param server_ip   the IP address of the server to connect to, as a
 *                    null-terminated string.
 * @param port        the port on the server to connect to.
 * @param context     a pointer to a place to store a context that will
 *                    be used by fgr_socket_connect_is_complete() and
 *                    fgr_socket_connect_stop().
 * @return            ESP_OK on success, -ESP_ERR_NOT_FINISHED
 *                    if the connection attempt has started but
 *                    has not yet completed, otherwise another
 *                    negative error code from esp_err_t.
 */
int32_t fgr_socket_connect_start(int sock, const char *server_ip,
                                 uint16_t port, void **context);

/** Check if a connection attempt that was started with a call
 * to fgr_socket_connect_start() has completed.
 *
 * You should call this function if fgr_socket_connect_start()
 * returned -ESP_ERR_NOT_FINISHED.  If this function returns
 * ESP_OK, you are done, you are connected; you do not need to
 * call fgr_socket_connect_stop(), you can just carry on and
 * use the connected socket.
 *
 * If this function returns -ESP_FAIL the connection attempt
 * has failed; you do not need to call fgr_socket_connect_stop()
 * (though there is no harm in doing so), you should probably
 * call fgr_socket_destroy() to close the socket and try again.
 *
 * If this function returns -ESP_ERR_NOT_FINISHED then
 * connection attempts are continuing in the background; you
 * may wait a while and call this function again or you
 * may choose to give up and call fgr_socket_connect_stop()
 * (then probably close the socket and try again).
 *
 * @param context     the same pointer as was passed to
 *                    fgr_socket_connect_start().
 * @param timeout_ms  how long to do the check for in milliseconds.
 * @return            ESP_OK on success, -ESP_ERR_NOT_FINISHED
 *                    if the connection attempt is continuing
 *                    in the background, otherwise another
 *                    negative error code from esp_err_t.
 */
int32_t fgr_socket_connect_is_complete(void **context, int32_t timeout_ms);

/** Stop connecting to a socket.
 *
 * You should call this function if either fgr_socket_connect_start()
 * or fgr_socket_connect_is_complete() returned -ESP_ERR_NOT_FINISHED
 * and you want to give up trying.
 *
 * @param context the same pointer as was passed to
 *                fgr_socket_connect_start().
*/
void fgr_socket_connect_stop(void **context);

/* ----------------------------------------------------------------
 * FUNCTIONS: "CHANNEL" COMPOUND OPERATIONS
 * -------------------------------------------------------------- */

/** Create and connect a [non-blocking] socket: this internally
 * calls fgr_socket_create(), fgr_socket_connect() and
 * fgr_socket_set_non_blocking().  fgr_socket_channel_stop() MUST
 * be called when the connection is no longer required and that
 * will close the socket, no need to call fgr_socket_destroy()
 * or close() on the socket.
 *
 * The term "channel" is employed only to make the function names
 * here obviously different from those above.
 *
 * The main motivation for using this function is actually to be
 * able to use fgr_socket_channel_maintain(): the context returned
 * can be used with fgr_socket_channel_maintain() to ensure that the
 * connection is rebuilt on a failure.  The usage pattern is:
 *
 * - call fgr_socket_channel_start(): makes a connection, populates sock
 *   and context.
 * - perform any additional socket configration (e.g call
 *   fgr_socket_enable_tcp_keep_alive() or use posix socket configuration
 *   functions directly); you may wish to put all of these configurations
 *   into a function that follows the signature fgr_socket_channel_cb_t:
 *   see the definition of fgr_socket_channel_maintain() for why.
 * - call fgr_socket_channel_maintain(): maintains the connection.
 * - when done, call fgr_socket_channel_stop() to closethe connection
 *   and the socket, freeing resources.
 *
 * @param server_ip   the IP address of the server as a null-terminated
 *                    string. IMPORTANT: this string is not copied,
 *                    it must remain static until fgr_socket_channel_stop()
 *                    is called.
 * @param port        the port on the server to connect to.
 * @param sock        a pointer to a place to put the socket. IMPORTANT:
 *                    if fgr_socket_channel_maintain() is called after this
 *                    function and the connection is re-established
 *                    due to a failure, sock will become invalid -
 *                    you must replace it with the new sock that will
 *                    be passed to cfg_cb() (see fgr_socket_channel_maintain()).
 * @param context     a pointer to a place to store a context.
 * @return            ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_channel_start(const char *server_ip, uint16_t port,
                                 int *sock, void **context);

/** May be called after fgr_socket_channel_start() and will ensure that a
 * connection is re-made on failure.  A task is created to
 * maintain the connection and cfg_cb() may be called from that task.
 *
 * @param context            the same pointer as was passed to
 *                           fgr_socket_channel_start().
 * @param heatbeat_seconds   how often to call hearbeat_cb() in seconds,
 *                           ignored if hearbeat_cb() is NULL.
 * @param heartbeat_cb       a callback that will be called every
 *                           heatbeat_seconds if there has been
 *                           no other activity; the callback should
 *                           send a heartbeat message to the far end;
 *                           must be non-NULL if heartbeat_seconds is
 *                           non-zero.  Note that the callback should
 *                           call fgr_socket_channel_activity() if sending
 *                           succeeds or fgr_socket_channel_failed()
 *                           if sending fails.
 * @param down_cb            a callback that will be called if the connection
 *                           has gone down and reconnection is taking a while.
 *                           The eventual success of reconnection is
 *                           indicated by cfg_cb() being called.
 * @param cfg_cb             a callback that will be called after the
 *                           connection has been recreated due to
 *                           a failure.  The value of sock passed to
 *                           cfg_cb() is the new socket and should replace
 *                           the sock returned by fgr_socket_channel_start().
 *                           You may perform any custom configuration
 *                           of the socket (e.g. calling
 *                           fgr_socket_enable_tcp_keep_alive()) in this
 *                           callback.  Note that this may be called even
 *                           if down_cb() has not been called should the
 *                           duration of the disconnect be a short one.
 * @param cb_param           user parameter to be passed to hearbeat_cb()
 *                           and cfg_cb() when they are called; may be NULL.
 *
 */
int32_t fgr_socket_channel_maintain(void **context,
                                    size_t heartbeat_seconds,
                                    fgr_socket_channel_cb_t heartbeat_cb,
                                    fgr_socket_channel_cb_t cfg_cb,
                                    fgr_socket_channel_down_cb_t down_cb,
                                    void *cb_param);

/** If fgr_socket_channel_maintain() has been called with
 * a heartbeat callback, you should call this function
 * whenever there is activity (i.e. a successful send or
 * a receive) on the socket.  This allows the hearbeat to
 * be skipped if there has been other activity.
 *
 * @param context      the same pointer as was passed to
 *                     fgr_socket_channel_start().
 */
void fgr_socket_channel_activity(void **context);

/** If fgr_socket_channel_maintain() has been called, calling this
 * function will trigger a reconnection attempt.
 *
 * @param context the same pointer as was passed to
 *                fgr_socket_channel_start().
 */
void fgr_socket_channel_failed(void **context);

/** This should be called when you are done with the connection
 * that was set up by fgr_socket_channel_start().  When this
 * function returns the socket returned by fgr_socket_channel_start()
 * will have been closed.
 *
 * @param context the same pointer as was passed to
 *                fgr_socket_channel_start().
 */
void fgr_socket_channel_stop(void **context);

/* ----------------------------------------------------------------
 * FUNCTIONS: SEND AND RECEIVE
 * -------------------------------------------------------------- */

/** Send data on a socket.
 *
 * @param sock           the socket.
 * @param buffer         a pointer to the data to send.
 * @param length         the amount of data in buffer.
 * @param retry_count    how many times to retry on failure, e.g.
 *                       FGR_SOCKET_TX_RETRY_COUNT.
 * @return               ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_socket_send(int sock, const void *buffer, size_t length,
                        size_t retry_count);

/** Start receiving data on a socket.  A task is created to receive
 * data and rx_cb() may be called from this task until
 * fgr_socket_receive_stop() is called.
 *
 * @param sock               the socket.
 * @param reconnect          a pointer to a Boolean which the receive
 *                           task will set to true if it believes that the
 *                           connection has failed. This pointer must
 *                           remain valid until fgr_socket_receive_stop()
 *                           is called.
 * @param reconnect_cb       callback to be called when the receive
 *                           task detects that the connection has gone
 *                           down, e.g. fgr_socket_channel_failed().
 * @param reconnect_cb_param user parameter to be passed to reconnect_cb()
 *                           when it is called: if your reconnect_cb() is
 *                           fgr_socket_channel_failed() then this should
 *                           be the context pointer that was passed to
 *                           fgr_socket_channel_start(); may be NULL.
 * @param rx_cb              callback to be called when data is received.
 * @param rx_cb_param        user parameter to be passed to rx_cb()
 *                           when it is called; may be NULL.
 * @param context            a pointer to a place to store a context.
 * @return                   ESP_OK on success, else a negative value
 *                           from esp_err_t.
 */
int32_t fgr_socket_receive_start(int sock,
                                 fgr_socket_reconnect_cb_t reconnect_cb,
                                 void **reconnect_cb_param,
                                 fgr_socket_rx_cb_t rx_cb,
                                 void *rx_cb_param, void **context);

/** Stop receiving data on a socket.  When this function has returned
 * reconnect_cb() and rx_cb() will no longer be called.
 *
 * @param context the same pointer as was passed to
 *                fgr_socket_receive_start().
 */
void fgr_socket_receive_stop(void **context);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_SOCKET_H_

// End of file
