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

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Function to call when a message is received.
 *
 * @param msg    a pointer to the message: this should be handled/
 *               copied/whatever before the callback returns.
 * @param param  cb_param as passed to fgr_msg_receive_start().
 */
typedef void (*fgr_msg_rx_cb_t)(fgr_msg_t *msg, void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS
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

/** Send a CNF message.
 *
 * @param cnf     the CNF message to send.
 * @param error   the error to send, 0 for success.
 * @param buffer  data to include in the message contents; may
 *                be NULL, must be non-NULL if length is non-zero.
 * @param length  the amount of data at buffer, ignored if
 *                buffer is NULL.
 * @return        ESP_OK on success, else a negative value
 *                from esp_err_t.
 */
int32_t fgr_msg_send_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
                         const void *buffer, size_t length);

/** Send an IND message.
 *
 * @param ind     the IND message to send.
 * @param state   the state to include.
 * @param buffer  data to include in the message contents; may
 *                be NULL, must be non-NULL if length is non-zero.
 * @param length  the amount of data at buffer, ignored if
 *                buffer is NULL.
 * @return        ESP_OK on success, else a negative value
 *                from esp_err_t.
 */
int32_t fgr_msg_send_ind(fgr_ind_rsp_t ind, fgr_state_t state,
                         const void *buffer, size_t length);

/** Start receiving messages.  A task is created to receive
 * messages and cb() may be called from this task until
 * fgr_msg_receive_stop() is called.
 *
 * @param cb        callback to be called when a message is
 *                  received.
 * @param cb_param  user parameter to be passed to cb()
 *                  when it is called; may be NULL.
 * @return          ESP_OK on success, else a negative value
 *                  from esp_err_t.
 */
int32_t fgr_msg_receive_start(fgr_msg_rx_cb_t cb, void *cb_param);

/** Stop receiving messages.  When this function has returned
 * cb() will no longer be called.
 */
void fgr_msg_receive_stop();

/** Deinitialise the messaging interface.
 */
void fgr_msg_deinit();

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MSG_H_

// End of file
