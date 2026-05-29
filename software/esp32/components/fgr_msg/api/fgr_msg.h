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

#ifndef _FGR_MSG_H_
#define _FGR_MSG_H_

/** @file
 * @brief API to exchange messages with the controller for a node of
 * the front garden railway.
 */

#ifdef __cplusplus
extern "C" {
#endif

#include "../../../../../protocol/fgr_protocol.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_MSG_SEND_QUEUE_LENGTH
// Suggested length for a message send queue
# define FGR_MSG_SEND_QUEUE_LENGTH 10
#endif

// Make a REQ message type from a REQ_CNF.
#define MSG_REQ(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_REQ << 12))

// Make a CNF message type from a REQ_CNF.
#define MSG_CNF(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_CNF << 12))

// Make an IND message type from an IND_RSP.
#define MSG_IND(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_IND << 12))

// Make a RSP message type from an IND_RSP.
#define MSG_RSP(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_RSP << 12))

// Mask off the top four bits of a message to get the underlying type
#define MSG_MASK(msg) (msg & 0x0fff)

// Determine if a message is a request
#define IS_MSG_REQ(msg) (((msg >> 12) == FGR_MSG_TYPE_REQ) ? true : false)

// Determine if a message is a response
#define IS_MSG_RSP(msg) (((msg >> 12) == FGR_MSG_TYPE_RSP) ? true : false)

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Function to call to handle a received message.
 *
 * @param msg    a pointer to the message: this should be handled/
 *               copied/whatever before the callback returns.
 * @param param  cb_param as passed to fgr_msg_receive_handler_add().
 * @return       true if the message is handled, false if it
 *               can be passed to subsequent handlers.
 */
typedef bool (*fgr_msg_receive_handler_cb_t)(fgr_msg_t *msg, void *param);

/** Function to call to obtain the state of a node.
 *
 * @param param  cb_param as passed to fgr_msg_init().
 * @return       the state of the node.
 */
typedef fgr_state_t (*fgr_msg_state_cb_t)(void *param);

/** Function to call to obtain an RSSI reading.
 *
 * @param param  cb_param as passed to fgr_msg_rssi_cb_set().
 * @return       the RSSI reading in dBm.
 */
typedef int8_t (*fgr_msg_rssi_cb_t)(void *param);

/** Function to call when the connection with the controller
 * goes up or down.
 *
 * @param upNotDown true if the connection is up, else false;
 *                  note that there is nothing you can do about
 *                  a connection being down, it will be being
 *                  recreated in the backgroud already
 * @param param     cb_param as passed to fgr_msg_connection_state_cb().
 */
typedef void (*fgr_msg_connection_state_cb_t) (bool upNotDOwn, void *param);

/** Function to call to when a message is sent.
 *
 * @param param  cb_param as passed to fgr_msg_send_cb_set().
 */
typedef void (*fgr_msg_send_cb_t)(void *param);

/** Function to call to when a message is received.
 *
 * @param param  cb_param as passed to fgr_msg_receive_cb_set().
 */
typedef void (*fgr_msg_receive_cb_t)(void *param);

/** Function to populate a buffer with what will be
 * the contents of an FGR_REQ_CNF_PING response to
 * the controller.
 *
 * @param buffer a pointer to the buffer to populate.
 * @param length the amount of storage at buffer, will
 *               normally be FGR_MSG_CONTENTS_MAX_LEN.
 * @param param  cb_param as passed to
                 fgr_msg_send_ping_body_cb().
 * @return       the amount of data coped into buffer.
 */
typedef uint32_t (*fgr_msg_send_ping_body_cb_t)(uint8_t *buffer,
uint32_t length,
void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS: INITIALISATION/DEINITIALISATION
 * -------------------------------------------------------------- */

/** Initialise the messaging interface: connects to the controller,
 * allowing messages to be exchanged.  Needs a task so fgr_util_init()
 * must have been called first.  It is always safe to call this at any
 * time; if already initialised it will do nothing and return success.
 *
 * Note: this will create a mutex that is never destroyed.
 *
 * @param server_ip         IP address of the server, e.g. 10.10.3.1.
 *                          IMPORTANT: this is NOT copied, it must remain
 *                          static until fgr_msg_deinit() is called.
 * @param port              the port on the server that is listening for
 *                          FGR protocol messages.
 * @param heartbeat_seconds how frequently to ensure a message is
 *                          sent in order to maintain the channel;
 *                          if this is zero then there will be no
 *                          way of detecting closure of a socket
 *                          by the far end.
 * @param cb                a function that will return the state
 *                          of the node.  This will be called
 *                          (a) to populate any indication messages sent
 *                          with fgr_msg_send_ind() or any heartbeat
 *                          messages sent automatically and (b) to
 *                          populate a uint8_t in the body of an
 *                          automatic ping confirmation message;
 *                          may be NULL, in which case
 *                          FGR_STATE_NOT_POPULATED will be used in
 *                          the indication messages and the body
 *                          of the ping confirmation message will
 *                          be empty.
 * @param cb_param          parameter that will be passed to cb()
 *                          when it is called; may be NULL.
 * @return                  ESP_OK on success, else a negative value
 *                          from esp_err_t.
 */
int32_t fgr_msg_init(const char *server_ip, uint16_t port,
size_t heartbeat_seconds, fgr_msg_state_cb_t cb,
void *cb_param);

/** Deinitialise the messaging interface; after this has been called
 * the state callback passed to fgr_msg_init() will no longer be called,
 * any RSSI callback registered with fgr_msg_rssi_cb() will no longer
 * be called and any message handler callbacks added through
 * fgr_msg_receive_handler_add() will be removed also.  It is always safe
 * to call this at any time.
 */
void fgr_msg_deinit();

/** Set a callback that will return an RSSI reading; once this has
 * been called the reading will be included in the body of the heartbeat
 * message.
 *
 * @param cb        a pointer to a function that will return the RSSI
 *                  reading; use NULL to cancel a prevous callback.
 * @param cb_param  parameter that will be passed to cb()
 *                  when it is called; may be NULL.
 * @return          ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_msg_rssi_cb_set(fgr_msg_rssi_cb_t cb, void *cb_param);

/* ----------------------------------------------------------------
 * FUNCTIONS: SENDING
 * -------------------------------------------------------------- */

/** Send a CNF message.
 *
 * @param cnf       the CNF message to send.
 * @param error     the error to send, 0 for success.
 * @param reference the reference to use in the message, copied
 *                  from the incoming request message that this
 *                  is confirming.
 * @param buffer    data to include in the message contents; may
 *                  be NULL, must be non-NULL if length is non-zero.
 * @param length    the amount of data at buffer, ignored if
 *                  buffer is NULL.
 * @return          ESP_OK on success, else a negative value from
 *                  esp_err_t.
 */
int32_t fgr_msg_send_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
uint8_t reference, const void *buffer,
size_t length);

/** Send an IND message.
 *
 * IMPORTANT: this will call the callback passed to fgr_msg_init()
 * to obtain the state of this node.  If that callback locks
 * you node's context, make sure that this function is not
 * called while that context is locked.
 *
 * @param ind     the IND message to send.
 * @param buffer  data to include in the message contents; may
 *                be NULL, must be non-NULL if length is non-zero.
 * @param length  the amount of data at buffer, ignored if
 *                buffer is NULL.
 * @return        ESP_OK on success, else a negative value from
 *                esp_err_t.
 */
int32_t fgr_msg_send_ind(fgr_ind_rsp_t ind, const void *buffer,
size_t length);

/** Initialise a send message queue: you may need one of these if
 * you intend to send messages from a message receive handler
 * (see section below).
 *
 * Note: this will create a mutex that is never destroyed.
 *
 * @param length  how many entries you would like the queue to be
 *                able to hold.  FGR_MSG_SEND_QUEUE_LENGTH is a
 *                good number.
 * @return        ESP_OK on success, else a negative value from
 *                esp_err_t.
 */
int32_t fgr_msg_send_queue_init(size_t length);

/** Queue a CNF message for transmission.  fgr_msg_send_queue_init()
 * must have been called for this to work.
 *
 * @param cnf       the CNF message to send.
 * @param error     the error to send, 0 for success.
 * @param reference the reference to use in the message, copied
 *                  from the incoming request message that this
 *                  is confirming.
 * @param buffer    data to include in the message contents; may
 *                  be NULL, must be non-NULL if length is non-zero.
 * @param length    the amount of data at buffer, ignored if
 *                  buffer is NULL.
 * @return          ESP_OK on success, else a negative value from
 *                  esp_err_t.
 */
int32_t fgr_msg_send_queue_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
uint8_t reference, const void *buffer,
size_t length);

/** Queue an IND message for transmission.  fgr_msg_send_queue_init()
 * must have been called for this to work
 *
 * @param ind     the IND message to send.
 * @param buffer  data to include in the message contents; may
 *                be NULL, must be non-NULL if length is non-zero.
 * @param length  the amount of data at buffer, ignored if
 *                buffer is NULL.
 * @return        ESP_OK on success, else a negative value from
 *                esp_err_t.
 */
int32_t fgr_msg_send_queue_ind(fgr_ind_rsp_t ind, const void *buffer,
size_t length);

/** Get how many messages are currently on the send queue.
 *
 * @return on success the number of queued send messages, else
 *         negative value from esp_err_t.
 */
int32_t fgr_msg_send_queue_size();

/** Destroy a send message queue that was created with
 * fgr_msg_send_queue_init(), freeing resources.  Any entries
 * currently on the queue are flushed, no transmission of those
 * messages will occur.
 *
 * fgr_msg_deinit() will call this for you.
 */
void fgr_msg_send_queue_deinit();

/** Set a callback to be called whenever a message is
 * successfully sent, either through this API or
 * automagically (e.g. the heartbeat message).  Note that
 * the callback is called in a blocking fashion after the
 * send, so don't do much in your callback unless you are
 * happy to delay message transmission.
 *
 * IMPORTANT: do not call into the msg API from the callback
 * as that will cause a deadlock.
 *
 * Note: there is no need to respond to FGR_REQ_CNF_PING
 * messages in your callback, those are automatically
 * responded to.  Instead call fgr_msg_send_ping_body_cb()
 * to supply a callback that will populate the body of the
 * ping confirmation if you wish.  If you DO handle
 * FGR_REQ_CNF_PING in your callback and return True to
 * indicate that the message has been handled that will
 * have the effect of cancelling the automatic ping
 * confirmation.
 *
 * @param cb        the callback; use NULL to cancel a previous
 *                  callback.
 * @param cb_param  parameter that will be passed to cb()
 *                  when it is called; may be NULL.
 * @return          ESP_OK on success, else a negative value
 *                  from esp_err_t.
 */
int32_t fgr_msg_send_cb_set(fgr_msg_send_cb_t cb, void *cb_param);

/** This library will automatically confirm a FGR_REQ_CNF_PING
 * message: with this function you may set a callback that
 * can supply the optional body to that confirmation.  If you
 * do not set a callback, the body of the FGR_REQ_CNF_PING
 * message that is automatically sent will be populated with a
 * uint8_t containing the current state as populated by the
 * callback passed to fgr_msg_init() (if set). If no callback
 * was passed to fgr_msg_init() the body of the
 * FGR_REQ_CNF_PING will be empty.
 *
 * IMPORTANT: do not call into the msg API from the callback as
 * that will cause a deadlock.
 *
 * @param cb        the callback; use NULL to cancel a previous
 *                  callback.
 * @param cb_param  parameter that will be passed to cb()
 *                  when it is called; may be NULL.
 * @return          ESP_OK on success, else a negative value
 *                  from esp_err_t.
 */
int32_t fgr_msg_send_ping_body_cb(fgr_msg_send_ping_body_cb_t cb,
void *cb_param);

/* ----------------------------------------------------------------
 * FUNCTIONS: RECEIVING
 * -------------------------------------------------------------- */

/** Start receiving messages.  A task is created to receive
 * messages; call fgr_msg_receive_handler_add() to get them,
 * call fgr_msg_receive_stop() when done.
 *
 * @return ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_msg_receive_start();

/** Set a callback to be called whenever a message is
 * received.  Note that this does not pass on the message to
 * be handled, for that see fgr_msg_receive_handler_add(),
 * it is only useful as a kind of "the link is alive" prompt,
 * for instance to pass to fgr_monitor_msg_receive_cb().
 *
 * IMPORTANT: do not call into the msg API from the callback
 * as that will cause a deadlock.
 *
 * @param cb        the callback; use NULL to cancel a previous
 *                  callback.
 * @param cb_param  parameter that will be passed to cb()
 *                  when it is called; may be NULL.
 * @return          ESP_OK on success, else a negative value
 *                  from esp_err_t.
 */
int32_t fgr_msg_receive_cb_set(fgr_msg_receive_cb_t cb, void *cb_param);

/** Add a message receive handler: when a message of the given
 * type has been decoded the handler will be called;  If the
 * handler returns true then the message is handled, if the
 * handler returns false the message will be offered to any
 * other handlers registered against the same message type.
 * Handlers added most recently have priority (i.e. the list
 * is populated from the head).  Try not to do too much in a
 * handler, nothing time consuming; each handler blocks message
 * reception.
 *
 * fgr_msg_receive_start() must have been called for any handlers
 * to be called.
 *
 * IMPORTANT: a message handler should not call back into
 * the msg API (aside from the debug print messages at the bottom)
 * since that could cause a deadlock.  Any calls to, for
 * instance, fgr_msg_send_cnf() or fgr_msg_send_ind(), should
 * be queued for execution after the handler has returned;
 * the functions fgr_msg_send_queue_*() are provided to help
 * you with this.
 *
 * @param msg_type  the message type that should cause the
 *                  handler to be called, i.e. one of
 *                  fgr_req_cnf_t or fgr_ind_rsp_t, with
 *                  the top 4 bits OR'ed with FGR_MSG_TYPE_REQ
 *                  or FGR_MSG_TYPE_RSP (no need for CNF or
 *                  IND since those are never received by
 *                  a node), native endian.  Use zero
 *                  to indicate all message types.
 *                  You may wish to use the MSG_CNF() and
 *                  MSG_RSP() macros to form this variable
 *                  from an fgr_req_cnf_t or a fgr_ind_rsp_t
 *                  type.
 * @param cb        handler to be called when a message
 *                  of type msg_type has been received.
 * @param cb_param  user parameter to be passed to cb()
 *                  when it is called; may be NULL.
 * @return          ESP_OK on success, else a negative value
 *                  from esp_err_t.
 */
int32_t fgr_msg_receive_handler_add(uint16_t msg_type,
fgr_msg_receive_handler_cb_t cb,
void *cb_param);

/** Remove a message receive handler that was added with
 * fgr_msg_receive_handler_add().  ALL message handlers with
 * the given address will be removed.  There is no need to
 * call this when exiting: fgr_msg_receive_stop() will
 * clean up.
 *
 * @param cb  address of the handler that was added with
 *            fgr_msg_receive_handler_add().
 */
void fgr_msg_receive_handler_remove_by_cb(fgr_msg_receive_handler_cb_t cb);

/** Remove a message receive handler that was added with
 * fgr_msg_receive_handler_add().  ALL message handlers with
 * the given message type will be removed.  There is no need to
 * call this when exiting: fgr_msg_receive_stop() will
 * clean up.
 *
 * @param msg_type  the message type for the handler(s) added
 *                  with fgr_msg_receive_handler_add().
 */
void fgr_msg_receive_handler_remove_by_type(uint16_t msg_type);

/** Stop receiving messages.  When this function has returned
 * cb() will no longer be called and all message handlers
 * will be removed.
 *
 * fgr_msg_deinit() will call this for you.
 */
void fgr_msg_receive_stop();

/* ----------------------------------------------------------------
 * FUNCTIONS: DEBUG
 * -------------------------------------------------------------- */

/** Populate a buffer with a string that is the name of the
 * given message type.  The returned string is guaranteed
 * to be null terminated.
 *
 * @param msg_type  the message type i.e. one of
 *                  fgr_req_cnf_t or fgr_ind_rsp_t, with
 *                  the top 4 bits OR'ed with FGR_MSG_TYPE_REQ
 *                  FGR_MSG_TYPE_CNF, FGR_MSG_TYPE_IND or
 *                  FGR_MSG_TYPE_RSP, native endian.
 * @param buffer    a buffer in which to store the message
 *                  name; allow at least 64 characters; cannot
 *                  be NULL.
 * @param length    the amount of storage at buffer, must be
 *                  non-zero.
 * @return          the length of the string written to buffer
 *                  (i.e. what strlen() would return) else
 *                  negative error code from esp_err_t.
 */
int32_t fgr_msg_name(uint16_t msg_type, char *buffer, size_t length);

/** Populate a buffer with the name of the error value
 * from a CNF message.  The returned string is guaranteed
 * to be null terminated.
 *
 * @param error   the error.
 * @param buffer  a buffer in which to store the name; allow
 *                at least 32 characters; cannot be NULL.
 * @param length  the amount of storage at buffer, must be
 *                non-zero.
 * @return        the length of the string written to buffer
 *                (i.e. what strlen() would return) else
 *                negative error code from esp_err_t.
 */
int32_t fgr_msg_error_name(fgr_error_t error, char *buffer, size_t length);

/** Populate a buffer with the name of the state value
 * from an IND message.  The returned string is guaranteed
 * to be null terminated.
 *
 * @param state   the state.
 * @param buffer  a buffer in which to store the name; allow
 *                at least 32 characters; cannot be NULL.
 * @param length  the amount of storage at buffer, must be
 *                non-zero.
 * @return        the length of the string written to buffer
 *                (i.e. what strlen() would return) else
 *                negative error code from esp_err_t.
 */
int32_t fgr_msg_state_name(fgr_state_t error, char *buffer, size_t length);

/** Print a summary of a message for debug purposes.
 *
 * @param prefix_str  pointer to a null-terminated prefix
 *                    string to add before the printed
 *                    summary.  If NULL the printed summary
 *                    is prefixed with "Sent" or "Received".
 * @param level       the log level to print the summary at.
 * @param msg_type    the message type i.e. one of
 *                    fgr_req_cnf_t or fgr_ind_rsp_t, with
 *                    the top 4 bits OR'ed with FGR_MSG_TYPE_REQ
 *                    FGR_MSG_TYPE_CNF, FGR_MSG_TYPE_IND or
 *                    FGR_MSG_TYPE_RSP, native endian.
 * @param error_state the error (for a CNF) or state (for an IND)
 *                    value from the message, ignored for
 *                    REQ or RSP messages.
 * @param reference   the message reference.
 * @param length      the amount of data included in the body
 *                    of the message.
 */
void fgr_msg_print_summary(const char *prefix_str, fgr_log_level_t level,
uint16_t msg_type, uint8_t error_state,
uint8_t reference, uint32_t length);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MSG_H_

// End of file
