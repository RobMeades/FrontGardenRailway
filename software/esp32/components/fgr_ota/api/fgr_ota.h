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
 * @brief The OTA API for the front garden railway: makes an
 * HTTP connection to a server and gets a file which is then
 * written to non-volatile storage and the system restarted.
 * Versions are checked and if all is good the download is
 * not performed, everything is left alone.
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
 
/** Initialise OTA, which basically involves setting up non-volatile
 * storage.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_ota_init();

/** Perform an OTA update.  Attempts to get the given file and,
 * if the version number (see version.txt in the main application)
 * is different to the current running code, will write the binary
 * file to NV storage and RESTART THE SYSTEM.  Requires networking
 * to have been established.  fgr_ota_init() must have been called
 * before this function.
 *
 * @param update_file_url   the URL of the binary file, e.g.
 *                          https://10.10.3.1:8070/stepper.bin.
 * @param server_cert_pem   a pointer to the start of the CA
 *                          certificate of the server.
 * @param timeout_ms        how long to hang around when downloading
 *                          the file in milliseconds, 5000 is a good
 *                          value.
 * @return                  ESP_OK on success, else a negative
 *                          value from esp_err_t.
 */
int32_t fgr_ota_update(const char *update_file_url,
                       const char *server_cert_pem,
                       int32_t timeout_ms);

#ifdef __cplusplus
}
#endif
 
/** @}*/
 
#endif // _FGR_OTA_H_
 
 // End of file
