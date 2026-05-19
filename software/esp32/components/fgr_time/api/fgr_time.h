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

#ifndef _FGR_TIME_H_
#define _FGR_TIME_H_

/** @file
 * @brief Time API for a node of the front garden railway.
 */

#ifdef __cplusplus
extern "C" {
#endif

#include "time.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_TIME_NTP_SERVER_IP_ADDRESS
// The IP address of an NTP server from which absolute time may
// be established.
#  define FGR_TIME_NTP_SERVER_IP_ADDRESS "178.62.68.79"
#endif

#ifndef FGR_TIME_NTP_SYNC_INTERVAL_SECONDS
// A suggested NTP resync interval (once per day).
#  define FGR_TIME_NTP_SYNC_INTERVAL_SECONDS (24 * 60 * 60)
#endif

// Posix DST string encoding the correct daylight saving time for the UK
#define FGR_TIME_TIMEZONE_LONDON "GMT0BST,M3.5.0/1,M10.5.0/2"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise time.
 *
 * Note: this will create a semaphore that is never destroyed.
 *
 * @param ntp_server_ip_address     a null terminated string that
 *                                  is the IP address of the NTP
 *                                  server from which to establish
 *                                  absolute time, e.g.
 *                                  FGR_TIME_NTP_SERVER_IP_ADDRESS;
 *                                  may be NULL in which case any
 *                                  of the APIs here that return
 *                                  absolute time, e.g. fgr_time_utc()
 *                                  and fgr_time_local() will fail.
 *                                  If this is non-NULL, make sure that
 *                                  the node has already established a
 *                                  network connection that permits
 *                                  outbound access to the IP address
 *                                  before this function is called.
 * @param timezone                  a null-terminated string defining
 *                                  the timezone in POSIX syntax, see
 *                                  for instance FGR_TIME_TIMEZONE_LONDON
 *                                  above; may be NULL, in which case
 *                                  local time will be UTC.
 * @param ntp_sync_interval_seconds the frequency at which to resync with
 *                                  the NTP server, e.g.
 *                                  FGR_TIME_NTP_SYNC_INTERVAL_SECONDS; use
 *                                  zero for never, ignored if
 *                                  ntp_server_ip_address is NULL.
 * @return                          ESP_OK on success, else a negative value
 *                                  from esp_err_t.
 */
int32_t fgr_time_init(const char *ntp_server_ip_address,
                      const char *timezone,
                      size_t ntp_sync_interval_seconds);

/** Deinitialise time.
 */
void fgr_time_deinit();

/** Get the time since boot in seconds.
 *
 * @return on success the time since boot in seconds,
 *         else negative value from esp_err_t.
 */
time_t fgr_time_since_boot();

/** Get the time since power-on in seconds; this only
 * returns a valid value if the time has been synchronised
 * with NTP.
 *
 * @return on success the time since power-on in seconds,
 *         else negative value from esp_err_t, and
 *         specifically -ESP_ERR_NOT_FOUND if NTP synchronisation
 *         has not yet occurred.
 */
time_t fgr_time_since_power_on();

/** Get the UTC time.
 *
 * @return on success the absolute time in seconds,
 *         else negative value from esp_err_t, and
 *         specifically -ESP_ERR_NOT_FOUND if absolute time
 *         has not been established.
 */
time_t fgr_time_utc();

/** Get the local time (applies timezone and DST automatically).
 *
 * @return on success the time in seconds, else negative
 *         value from esp_err_t, and specifically
 *         -ESP_ERR_NOT_FOUND if absolute time has not been
 *         established.
 */
time_t fgr_time_local();

/** @}*/

#endif // _FGR_TIME_H_