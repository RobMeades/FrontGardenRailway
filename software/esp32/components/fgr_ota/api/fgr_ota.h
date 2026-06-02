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

#ifndef _FGR_OTA_H_
#define _FGR_OTA_H_

/** @file
 * @brief The OTA API for a node of the front garden railway: makes
 * an HTTP connection to a server and gets a file which is then
 * written to non-volatile storage and the system restarted.
 * Versions are checked and if all is good the download is
 * not performed, everything is left alone.  Relies on the library
 * fgr_nvs.
 */

#ifdef __cplusplus
extern "C" {
#endif

#include "fgr_util.h" // for fgr_util_cb_t

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_OTA_VERIFY_TIME_SECONDS
// How long to wait to get "is good" responses from the various
// systems after booting a new image; should be a relatively
// long time as it relies on Wifi making connections.
#    define FGR_OTA_VERIFY_TIME_SECONDS (60 * 3)
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Function to call to determine if a library is good.
 *
 * @return       true if all is good, else false.
 */
typedef bool (*fgr_ota_lib_is_good_cb_t) ();

/** Function to call to determine if the application is good.
 *
 * @param param  cb_param as passed to fgr_ota_init().
 * @return       true if all is good, else false.
 */
typedef bool (*fgr_ota_app_is_good_cb_t) (void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise OTA; fgr_nvs_init() MUST NOT have been called for
 * this to work - a call to fgr_nvs_init() is daisy-chained onto
 * the end of this function, the ESP-IDF OTA stuff just works that
 * way.  This function should be called ONCE at start of day.
 *
 * The varous callbacks are used to determine that all is good after
 * booting into a new image; all must return true within
 * FGR_OTA_VERIFY_TIME_SECONDS to stop a roll-back occurring.  If
 * all of the callbacks are NULL then no roll-back will occur.
 *
 * @param msg_is_good_cb  callback to call to determine if messaging
 *                        is good, meaning it is connected to the
 *                        controller; may be NULL.
 * @param log_is_good_cb  callback to call to determine if logging
 *                        is good, meaning it is connected to the
 *                        log server; may be NULL.
 * @param app_is_good_cb  callback to call to determine if the
 *                        applcation is good; may be NULL.
 * @param cb              a callback that will be called just before
 *                        any roll-back is initiated; use this to
 *                        perform absolutely necessary tidy-ups in
 *                        your application, noting that the system
 *                        may be unstable at the time otherwise we
 *                        wouldn't be rolling-back  You only need
 *                        to call any of the various library
 *                        xxx_deinit() calls if you chose to
 *                        initialise libraries individually, rather
 *                        than using fgr_lib_init(), and even then
 *                        it may not be worth it, we're going down.
 *                        May be NULL.
 * @param cb_param        a parameter to pass to cb() and
 *                        app_is_good_cb(); may be NULL.
 * @return                ESP_OK on success, else a negative value
 *                        from esp_err_t.
 */
int32_t fgr_ota_init(fgr_ota_lib_is_good_cb_t msg_is_good_cb,
                     fgr_ota_lib_is_good_cb_t log_is_good_cb,
                     fgr_ota_app_is_good_cb_t app_is_good_cb,
                     fgr_util_cb_t cb, void *cb_param);

/** Perform an OTA update.  Attempts to get the given file and,
 * if the version number (see version.txt in the main application)
 * is different to the current running code, will write the binary
 * file to NV storage and RESTART THE SYSTEM.  Requires networking
 * to have been established.  fgr_ota_init() must have been called
 * before this function.
 *
 * Note: since this may run for a little while it internally calls
 * fgr_monitor_task_wdt_feed().
 *
 * @param update_file_url   the URL of the binary file, e.g.
 *                          https://10.10.3.1:8070/default.bin,
 *                          or, if you are running https_server.py
 *                          in differentaited mode,
 *                          https://10.10.3.1:8070/update;
 *                          cannot be NULL.
 * @param server_cert_pem   a pointer to the start of the CA
 *                          certificate of the server, cannot be NULL.
 * @param timeout_ms        how long to hang around when downloading
 *                          the file in milliseconds, 5000 is a good
 *                          value.
 * @return                  ESP_OK on success, else a negative
 *                          value from esp_err_t.
 */
int32_t fgr_ota_update(const char *update_file_url, const char *server_cert_pem,
                       int32_t timeout_ms);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_OTA_H_

// End of file
