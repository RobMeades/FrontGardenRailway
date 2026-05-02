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

// Make a REQ message type from a REQ_CNF.
#define MSG_REQ(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_REQ` << 12))

// Make a CNF message type from a REQ_CNF.
#define MSG_CNF(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_CNF` << 12))

// Make an IND message type from an IND_RSP.
#define MSG_IND(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_IND << 12))

// Make a RSP message type from an IND_RSP.
#define MSG_RSP(msg) ((msg & 0x0fff) | (FGR_MSG_TYPE_RSP << 12))

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Function to call when a message is received.
 *
 * @param msg    a pointer to the message: this should be handled/
 *               copied/whatever before the callback returns.
 * @param param  cb_param as passed to fgr_msg_receive_handler_add().
 * @return       true if the message is handled, false if it
 *               can be passed to subsequent handlers.
 */
typedef bool (*fgr_msg_rx_cb_t)(fgr_msg_t *msg, void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS: INITIALISATION/DEINITIALISATION
 * -------------------------------------------------------------- */

/** Initialise the messaging interface.
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
 * @param state             a pointer to a place to get the state
 *                          to put in the heartbeat message; ignored
 *                          if heartbeat_seconds is zero, may be NULL
 *                          (in which case FGR_STATE_NOT_POPULATED
 *                          will be used).  IMPORTANT: this pointer
 *                          must remain valid until fgr_msg_deinit()
 *                          is called.
 * @return                  ESP_OK on success, else a negative value
 *                          from esp_err_t.
 */
int32_t fgr_msg_init(const char *server_ip, uint16_t port,
                     size_t heartbeat_seconds, fgr_state_t *state);

/** Deinitialise the messaging interface.
 */
void fgr_msg_deinit();

/* ----------------------------------------------------------------
 * FUNCTIONS: SENDING
 * -------------------------------------------------------------- */

/** Send a CNF message.
 *
 * @param cnf     the CNF message to send.
 * @param error   the error to send, 0 for success.
 * @param buffer  data to include in the message contents; may
 *                be NULL, must be non-NULL if length is non-zero.
 * @param length  the amount of data at buffer, ignored if
 *                buffer is NULL.
 * @return        on success the reference that was used in
 *                the message, else a negative value from esp_err_t.
 */
int32_t fgr_msg_send_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
                         const void *buffer, size_t length);

/** Send an IND message.
 *
 * @param ind     the IND message to send.
 * @param state   the state to include, 0 if there is no state.
 * @param buffer  data to include in the message contents; may
 *                be NULL, must be non-NULL if length is non-zero.
 * @param length  the amount of data at buffer, ignored if
 *                buffer is NULL.
 * @return        on success the reference that was used in
 *                the message, else a negative value from esp_err_t.
 */
int32_t fgr_msg_send_ind(fgr_ind_rsp_t ind, fgr_state_t state,
                         const void *buffer, size_t length);

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
                                    fgr_msg_rx_cb_t cb,
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
void fgr_msg_receive_handler_remove_by_cb(fgr_msg_rx_cb_t cb);

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
void fgr_msg_print_summary(uint16_t msg_type, uint8_t error_state,
                           uint8_t reference, uint32_t length);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MSG_H_

// End of file
