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

#ifndef _FGR_UTIL_H_
#define _FGR_UTIL_H_

 /** @file
  * @brief Utilites API for a node of the front garden railway.
  */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#define FGR_UTIL_ARRAY_LENGTH(array) (sizeof(array) / sizeof(array[0]))

#ifndef FGR_UTIL_WATCHDOG_FEED_TIME_MS
// The number of milliseconds to vTaskDelay() for in order to let the idle task
// to feed its watchdog
#  define FGR_UTIL_WATCHDOG_FEED_TIME_MS 10
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_UTIL_H_

// End of file
