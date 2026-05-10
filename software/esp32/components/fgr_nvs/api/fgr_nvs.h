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

#ifndef _FGR_NVS_H_
#define _FGR_NVS_H_

/** @file
 * @brief The NVSA API for a node of the front garden railway: just
 * init really.
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

/** Initialise NVS. If NVS has already been initialised this will
 * do nothing and return success.
 *
 * IMPORTANT: if you are using fgr_ota do NOT call this: OTA
 * needs to initialise first and it will then, internally, call
 * fgr_nvs_init();
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_nvs_init();

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_NVS_H_

 // End of file
