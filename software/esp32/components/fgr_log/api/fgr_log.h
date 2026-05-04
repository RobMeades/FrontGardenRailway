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

#ifndef _FGR_LOG_H_
#define _FGR_LOG_H_

/** @file
 * @brief API for a node of the front garden railway to handle forwarding
 * of logs to the controller.
 */

#ifdef __cplusplus
extern "C" {
#endif

// Required for fgr_log_level_t and fgr_msg_t.
#include "../../../../../protocol/fgr_protocol.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise logging to a remote server; requires networking to
 * be up first.  May be safely called at any time: will return
 * success if fgr_log_init() has already been called.
 *
 * Note: this will create a mutex that is never destroyed.
 *
 * @param server_ip IP address of the server, e.g. 10.10.3.1;
 *                  note that this is NOT copied, it must remain
 *                  static until fgr_log_deinit() is called.
 * @param port      the port on the server that is listening for
 *                  log messages.
 * @param min_level the minimum level to log (default LOG_INFO).
 * @return          ESP_OK on success, else a negative value from
 *                  esp_err_t.
 */
int32_t fgr_log_init(const char *server_ip, uint16_t port, fgr_log_level_t min_level);

/** Return back to the normal ESP32 logging.
 */
void fgr_log_deinit();

/** Change the minimum log level.
 *
 * @param level the new minimum level to log.
 * @return      ESP_OK on success, else a negative value from
 *              esp_err_t.
 */
int32_t fgr_log_set_min_level(fgr_log_level_t level);

/** Stop logging.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_log_off();

/** Turn logging back on.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_log_on();

/**  A message receive callback that will handle
 * the FGR_REQ_CNF_LOG_* messages: add this to your
 * application's message receive chain (before
 * your own handlers so that it is below them) with:
 *
 * fgr_msg_receive_handler_add(0, fgr_log_msg_receive_cb, NULL);
 *
 * ...and this code will deal with them for you.
 *
 * IMPORTANT: for this to work your application must
 * set up a message send queue (i.e. must have called
 * fgr_msg_send_queue_init()).
 */
bool fgr_log_msg_receive_cb(fgr_msg_t *msg, void *param);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_LOG_H_

// End of file
