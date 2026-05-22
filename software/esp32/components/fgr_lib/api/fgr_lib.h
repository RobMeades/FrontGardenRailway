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

#ifndef _FGR_LIB_H_
#define _FGR_LIB_H_

/** @file
 * @brief General library-wide functions for the front garden railway.
 */

#ifdef __cplusplus
extern "C" {
#endif

// Needed for fgr_msg_state_cb_t and fgr_msg_send_cb_t,
#include "fgr_msg.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_LIB_INITIALISATION_DELAY_SECONDS
// The Raspberry Pi access point can get upset if it sees a node
// disconnect without clean-up and reconnect frequently,
// therefore we pause at boot to make it happy.
#  define FGR_LIB_INITIALISATION_DELAY_SECONDS 5
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise all libraries.  Should only be called ONCE at start
 * of day.  Note that there will be a delay of
 * FGR_LIB_INITIALISATION_DELAY_SECONDS before acting.
 *
 * Note: this will also enable the task watchdog.
 *
 * @param ota_server_cert_pem   a pointer to the start of the CA
 *                              certificate of the OTA server.
 * @param state_cb              a function that will return the state
 *                              of the node.  This will be called
 *                              (a) to populate any indication messages sent
 *                              with fgr_msg_send_ind() or any heartbeat
 *                              messages sent automatically and (b) to
 *                              populate a uint8_t in the body of an
 *                              automatic ping confirmation message;
 *                              may be NULL, in which case
 *                              FGR_STATE_NOT_POPULATED will be used in
 *                              the indication messages and the body
 *                              of the ping confirmation message will
 *                              be empty.
 * @param send_cb               a callback to be called whenever a message is
 *                              successfully sent, either through this API or
 *                              automagically (e.g. the heartbeat message).
 *                              Note that the callback is called in a blocking
 *                              fashion after the* send, so don't do much in
 *                              your callback unless you are* happy to delay
 *                              message transmission.
 * @param cb                    a callback that will be called just before
 *                              fgr_monitor calls abort() to cause a system
 *                              restart; use this to do any absolutely
 *                              necessary tidy-ups in your application, noting
 *                              that the system may be unstable at the time,
 *                              otherwise the monitor task wouldn't be calling
 *                              abort().  You don't need to call fgr_lib_deinit()
 *                              (fgr_monitor will do that), though there is no
 *                              harm in doing so.  May be NULL.
 * @param cb_param              parameter that will be passed to state_cb()
 *                              and send_cb() when they are called; may be NULL.
 * @return                      ESP_OK on success, else a negative value
 *                              from esp_err_t.

 */
int32_t fgr_lib_init(const char *ota_server_cert_pem,
                     fgr_msg_state_cb_t state_cb, fgr_msg_send_cb_t send_cb,
                     fgr_monitor_cb_t monitor_cb, void *cb_param);

/** Deinitialise all libraries.  Should only be called ONCE at end of day.
 */
void fgr_lib_deinit();

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_LIB_H_

// End of file
