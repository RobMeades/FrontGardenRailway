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

#ifndef _FGR_MONITOR_H_
#define _FGR_MONITOR_H_

 /** @file
  * @brief Monitoring API for a node of the front garden railway.
  */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_MONITOR_WDT_TIMEOUT_SECONDS_ADVANCE
// The number of seconds before CONFIG_ESP_TASK_WDT_TIMEOUT_S
// that the monitor task watchdog timeout will be set to.
// If CONFIG_ESP_TASK_WDT_TIMEOUT_S is not defined then set
// use FGR_MONITOR_WDT_TIMEOUT_SECONDS instead.
#  define FGR_MONITOR_WDT_TIMEOUT_SECONDS_ADVANCE 5
#endif

#ifndef FGR_MONITOR_WDT_TIMEOUT_SECONDS
// The duration the monitor task watchdog timeout will be set to,
// user 0 for no WDT monitoring.
#  if defined (CONFIG_ESP_TASK_WDT_TIMEOUT_S)
#    define FGR_MONITOR_WDT_TIMEOUT_SECONDS (CONFIG_ESP_TASK_WDT_TIMEOUT_S - FGR_MONITOR_WDT_TIMEOUT_SECONDS_ADVANCE)
#  else
#    define FGR_MONITOR_WDT_TIMEOUT_SECONDS 10
#  endif
#endif

#if (FGR_MONITOR_WDT_TIMEOUT_SECONDS < 0)
#  error "CONFIG_ESP_TASK_WDT_TIMEOUT_S, if defined, must be at least FGR_MONITOR_WDT_TIMEOUT_SECONDS_ADVANCE, or if not defined then FGR_MONITOR_WDT_TIMEOUT_SECONDS must be non-negative"
#endif

#ifndef FGR_MONITOR_CHECK_INTERVAL_MS
// How frequently to do a check on stuff.
#  define FGR_MONITOR_CHECK_INTERVAL_MS 250
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** A callback to the main application that will be called just
 * before abort().
 *
 * @param param  cb_param as passed to fgr_monitor_init().
 */
typedef void (*fgr_monitor_cb_t)(void *param);


/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise monitoring.  fgr_task_init() must have been called
 * before this function (since tasks are monitored).  This function
 * must be called once at start of day.
 *
 * Note: there is no fgr_monitor_deinit(), this creates a
 * couple of semaphores and a task that are never destroyed.
 *
 * @param cb         a callback that will be called just before
 *                   the monitor task calls abort() to cause
 *                   a system restart; use this to do any absolutely
 *                   necessary tidy-ups in your application, noting
 *                   that the system may be unstable at the time,
 *                   otherwise the monitor task wouldn't be calling
 *                   abort().  You only need to call any of the
 *                   various library xxx_deinit() calls if you chose to
 *                   initialise libraries individually, rather than
 *                   using fgr_lib_init(), and even then it's
 *                   probably not worth it, we're going down...
 *                   May be NULL.
 * @param cb_param   a parameter to pass to the callback; may be NULL.
 * @return           ESP_OK on success, else a negative value from
 *                   esp_err_t.
 */
int32_t fgr_monitor_init(fgr_monitor_cb_t cb, void *cb_param);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MONITOR_H_

// End of file
