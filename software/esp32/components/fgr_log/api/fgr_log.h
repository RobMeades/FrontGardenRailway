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

#ifndef FGR_LOG_BUFFER_MAX_ENTRIES
/** Maximum number of log entries to buffer when disconnected from the
 *  log server. Each entry consumes approximately 100-200 bytes of RAM
 *  (including header and message body). With PSRAM available on ESP32-S3,
 *  2048 entries is reasonable (~400KB). Without PSRAM, reduce to 256.
 */
# define FGR_LOG_BUFFER_MAX_ENTRIES 2048
#endif

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
 * If fgr_nvs_init() or fgs_ota_init() have been called before this
 * function then the log level setting and whether logging is on or
 * off will be stored in non-volatile storage for use on
 * subsequent boots.
 *
 * Note: this will create a mutex that is never destroyed.
 *
 * @param server_ip IP address of the server, e.g. 10.10.3.1;
 *                  note that this is NOT copied, it must remain
 *                  static until fgr_log_deinit() is called.
 * @param port      the port on the server that is listening for
 *                  log messages.
 * @param level_min the minimum level to log (default LOG_INFO);
 *                  if fgr_nvs_init() or fgs_ota_init() have been
 *                  called and there is a saved log level then this
 *                  value will be ignored: use fgr_log_set_level_min()
 *                  to set it.
 * @return          ESP_OK on success, else a negative value from
 *                  esp_err_t.
 */
int32_t fgr_log_init(const char *server_ip, uint16_t port, fgr_log_level_t level_min);

/** Return back to the normal ESP32 logging.  It is always safe to call
 * this at any time.
 */
void fgr_log_deinit();

/** Change the minimum log level.  If fgr_nvs_init() or fgs_ota_init()
 * have been called then the value will be saved and used on the next boot.
 *
 * @param level the new minimum level to log.
 * @return      ESP_OK on success, else a negative value from
 *              esp_err_t.
 */
int32_t fgr_log_set_level_min(fgr_log_level_t level);

/** Stop logging.  If fgr_nvs_init() or fgs_ota_init() hav been called
 * then the log setting will persist across boot cycles.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_log_off();

/** Turn logging on.  If fgr_nvs_init() or fgs_ota_init() have been
 * called then the log setting will persist across boot cycles.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_log_on();

/** A message receive handler callback that will handle
 * the FGR_REQ_CNF_LOG_* messages: add this to your
 * application's message receive chain (before
 * your own handlers so that it is below them) with:
 *
 * fgr_msg_receive_handler_add(0, fgr_log_msg_receive_handler_cb, NULL);
 *
 * ...and this code will deal with them for you.
 *
 * IMPORTANT: for this to work your application must
 * set up a message send queue (i.e. must have called
 * fgr_msg_send_queue_init()).
 *
 * @param msg    a pointer to the received message.
 * @param param  cb_param as passed to fgr_msg_receive_handler_add().
 * @return       true if the message is handled, false if it
 *               can be passed to subsequent handlers.
 */
bool fgr_log_msg_receive_handler_cb(fgr_msg_t *msg, void *param);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_LOG_H_

// End of file
