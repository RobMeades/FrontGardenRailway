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

#ifndef _FGR_NETWORK_H_
#define _FGR_NETWORK_H_

/** @file
 * @brief The networking API for a node of the front garden railway.
 */

#ifdef __cplusplus
extern "C" {
#endif

// Required for wifi_auth_mode_t.
#include "esp_wifi_types.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise networking; requires the default event loop to
 * have been created.
 *
 * Note: this will create a mutex that is never destroyed.
 *
 * Note: since this may run for a little while it internally sets
 * the task watchdog to 60 seconds (and sets it back to
 * CONFIG_ESP_TASK_WDT_TIMEOUT_S afterwards).
 *
 * @param ssid             the SSID of the Wi-Fi access point to
 *                         connect to e.g. FGR.
 * @param password         the password to apply when connecting to
 *                         the access point, must be NULL or an empty
 *                         string ("") if auth_mode is WIFI_AUTH_OPEN.
 * @param auth_mode        the Wi-Fi authetication mode to use when
 *                         connecting to the access point.
 * @param reduced_tx_power in cases where the ESP32 board and the AP
 *                         are in close proximit y(e.g. less than a metre
 *                         apart), the connection can be more reliable
 *                         if the ESP32 uses a reduced TX power (max
 *                         8 dBm).
 * @return          ESP_OK on success, else a negative value from
 *                  esp_err_t.
 */
int32_t fgr_network_init(const char *ssid, const char *password,
                         wifi_auth_mode_t auth_mode,
                         bool reduced_tx_power);

/** Deinitialise networking.
 */
void fgr_network_deinit();

/** Determine if networking is connected.
 *
 * @return true if networking is connected, else false.
 */
bool fgr_network_is_connected();

/** Return a hostname string from a URL.  The string is
 * returned in the given buffer and the string length
 * (i.e. what strlen() would return) is the return value.
 * The value is guaranteed to be a string if the return
 * value is non-negative.

 *
 * IMPORTANT the return value may be larger than
 * buffer_len if buffer is not big enough to hold the
 * the (null terminated) host name, it is up to you to check
 * that the return value is at least one byte smaller (to
 * allow for the null terminator) than length.
 *
 * @param url        the url string, e.g. HTTPS://blah:port/something.
 * @param buffer     the buffer to put the hostname (blah) into.
 * @param length     the number of bytes of storage at buffer.
 * @return           the number of bytes written to buffer.
 */
size_t fgr_network_hostname_from_url(const char *url, char *buffer, size_t length);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _NETWORK_H_

// End of file
