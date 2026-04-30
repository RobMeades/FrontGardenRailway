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

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise the messaging interface.
 *
 * Note: this will create a mutex that is never destroyed.
 *
 * @param server_ip IP address of the server, e.g. 10.10.3.1.
 *                  IMPORTANT: this is NOT copied, it must remain
 *                  static until fgr_msg_deinit() is called.
 * @param port      the port on the server that is listening for
 *                  FGR protocol messages.
 * @return          ESP_OK on success, else a negative value from
 *                  esp_err_t.
 */
int32_t fgr_msg_init(const char *server_ip, uint16_t port);

/** Deinitialise the messaging interface.
 */
void fgr_msg_deinit();

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MSG_H_

// End of file
