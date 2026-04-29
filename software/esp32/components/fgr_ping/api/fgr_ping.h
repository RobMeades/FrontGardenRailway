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

#ifndef _FGR_PING_H_
#define _FGR_PING_H_
 
 /** @file
  * @brief The ping API for the front garden railway.
  */
 
#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Function to call on data loss.
 *
 * @param param  ping_loss_cb_param as passed to fgr_ping_start().
 */
typedef void (*fgr_ping_loss_cb_t)(void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */
 
/** Start a ping session, of default type, to the given host name.
 * Networking must have been started.  Pinging will continue in the
 * background and diagnostic prints will be emitted.
 *
 * @param hostname           the host name, a null-terminated string.
 * @param count              the number of pings to send; set a
 *                           negative value to use the default (5),
 *                           0 to ping forever.
 * @param interval_ms        the interval between pings in milliseconds;
 *                           set a negative value to use the default
 *                           of 1 second.
 * @param timeout_ms         the time to wait for a response in
 *                           milliseconds; set a negative value to
 *                           use the default of 1 second.
 * @param data_size          the number of bytes of data to send in
 *                           each ping; set a negative value to use
 *                           the default of 64.
 * @param ping_loss_cb       function to call on the loss of a ping;
 *                           may be NULL.
 * @param ping_loss_cb_param user parameter that will be passed to
 *                           ping_loss_cb() if it is called; may
 *                           be NULL.
 * @return                   ESP_OK on successful start, else a
 *                           negative value from esp_err_t.
 */
int32_t fgr_ping_start(const char *hostname, int32_t count,
                       int32_t interval_ms, int32_t timeout_ms,
                       int32_t data_size,
                       fgr_ping_loss_cb_t ping_loss_cb,
                       void *ping_loss_cb_param);

#ifdef __cplusplus
}
#endif
 
/** @}*/
 
#endif // _FGR_PING_H_
 
// End of file
