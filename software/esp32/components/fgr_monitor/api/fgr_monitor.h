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

#ifndef FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE
// The number of seconds before CONFIG_ESP_TASK_WDT_TIMEOUT_S
// that the monitor task watchdog timeout will be set to.
// If CONFIG_ESP_TASK_WDT_TIMEOUT_S is not defined then
// FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS apples instead.
#  define FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE 5
#endif

#ifndef FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_MAX
// The maximum value of the monitor task watchdog timeout (given
// that the maximum value of the ESP-IDF HW watchdog is 60 seconds).
#  define FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_MAX (60 - FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE)
#endif

#ifndef FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS
// The duration the monitor task watchdog timeout will be set to,
// user 0 for no WDT monitoring.
#  if defined (CONFIG_ESP_TASK_WDT_TIMEOUT_S)
#    define FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS (CONFIG_ESP_TASK_WDT_TIMEOUT_S - FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE)
#  else
#    define FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS 10
#  endif
#endif

#if FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS < 0
#  error "CONFIG_ESP_TASK_WDT_TIMEOUT_S, if defined, must be at least FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE, or if not defined then FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS must be non-negative"
#endif

#ifndef FGR_MONITOR_WDT_CONTROLLER_TIMEOUT_SECONDS
// The maximum amount of time between messages received from
// the controller in seconds: suggested 3 * CONFIG_FGR_MSG_HEARTBEAT_SECONDS
// plus a few seconds
#  if defined (CONFIG_FGR_MSG_HEARTBEAT_SECONDS)
#    define FGR_MONITOR_WDT_CONTROLLER_TIMEOUT_SECONDS ((CONFIG_FGR_MSG_HEARTBEAT_SECONDS * 3) + 5)
#  else
#    define FGR_MONITOR_WDT_CONTROLLER_TIMEOUT_SECONDS 300
#  endif
#endif

#ifndef FGR_MONITOR_TASK_STACK_SIZE_MIN
// How little task stack there can be for a before aborting.
#  define FGR_MONITOR_TASK_STACK_SIZE_MIN 128
#endif

#ifndef FGR_MONITOR_HEAP_MIN
// The minimum expected free heap (see also FGR_MONITOR_HEAP_MIN_DURATION_SECONDS).
#  define FGR_MONITOR_HEAP_MIN (10 * 1024)
#endif

#ifndef FGR_MONITOR_HEAP_MIN_DURATION_SECONDS
// How long the heap must have gone below FGR_MONITOR_HEAP_MIN to cause an abort.
#  define FGR_MONITOR_HEAP_MIN_DURATION_SECONDS 10
#endif

#ifndef FGR_MONITOR_HEAP_BLOCK_MIN
// The minimum expected free heap block size (guarding against fragmentation).
#  define FGR_MONITOR_HEAP_BLOCK_MIN 1024
#endif

#ifndef FGR_MONITOR_CHECK_INTERVAL_MS
// How frequently to do a check on stuff.
#  define FGR_MONITOR_CHECK_INTERVAL_MS 250
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** The abort reasons; negative so as not to overlap with any
 * user reason passed into fgr_monitor_abort().  If you change
 * this enum you must also update g_abort_reason_name[] in the
 * implementation.
 */
typedef enum {
    FGR_MONITOR_ABORT_REASON_USER_LAST = -1,
    FGR_MONITOR_ABORT_REASON_NONE = FGR_MONITOR_ABORT_REASON_USER_LAST,
    FGR_MONITOR_ABORT_REASON_TASK_LOW_STACK = -2,
    FGR_MONITOR_ABORT_REASON_LOW_HEAP = -3,
    FGR_MONITOR_ABORT_REASON_FRAGMENTED_HEAP = -4,
    FGR_MONITOR_ABORT_REASON_TASK_WDT = -5,
    FGR_MONITOR_ABORT_REASON_CONTROLLER_WDT = -6
} fgr_monitor_abort_reason_t;

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
 * before this function (since tasks created by fgr_task_create()
 * are monitored).  This function must be called once at start of day.
 *
 * Note: there is no fgr_monitor_deinit(), this creates a
 * couple of semaphores and a task that are never destroyed.
 *
 * @param cb         a callback that will be called just before
 *                   the monitor task calls abort() when it is doing
 *                   a system restart; use this to perform absolutely
 *                   necessary tidy-ups in your application, noting
 *                   that the system may be unstable at the time,
 *                   otherwise the monitor task wouldn't be calling
 *                   abort().  You only need to call any of the
 *                   various library xxx_deinit() calls if you chose to
 *                   initialise libraries individually, rather than
 *                   using fgr_lib_init(), and even then it may
 *                   not be worth it, we're going down. May be NULL.
 * @param cb_param   a parameter to pass to the callback; may be NULL.
 * @return           ESP_OK on success, else a negative value from
 *                   esp_err_t.
 */
int32_t fgr_monitor_init(fgr_monitor_cb_t cb, void *cb_param);

/** Feed the monitor task watchdog; this will also reset the
 * ESP-IDF HW watchdog, and will do so even if fgr_monitor_init()
 * has not been called, hence it is always safe to replace
 * any occurrences of esp_task_wdt_reset(); if you wish to
 * reset the watchdog in a task that was _not_ created by
 * fgr_task_create(), just use NULL and only the ESP-IDF
 * HW watchdog will be fed.
 *
 * @param handle  the handle of the task, as returned by
 *                fgr_task_create(), else NULL if the
 *                task was not created by fgr_task_create().
 */
void fgr_monitor_task_wdt_feed(void *handle);

/** Get the current monitor task watchdog timeout in seconds.
 *
 * @return on success the current value of the monitor task
 *         watchdog timeout in seconds, else negative error code
 *         from esp_err_t.
 */
int32_t fgr_monitor_task_wdt_timeout_get();

/** Set the current monitor task watchdog timeout; if
 * CONFIG_ESP_TASK_WDT_TIMEOUT_S is defined, this will
 * also set the ESP-IDF HW watchdog timeout and will set it
 * to FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE longer than
 * the value given.  If not called, FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS
 * applies.
 *
 * @param timeout_seconds the timeout to set in seconds; there is
 *                        an upper limit of
 *                        FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_MAX;
 *                        values larger than this will be limited
 *                        to this.
 * @return                on success the new monitor task watchdog
 *                        timeout in seconds, else negative error
 *                        code from esp_err_t.
 */
int32_t fgr_monitor_task_wdt_timeout_set(int32_t timeout_seconds);

/** A message receive callback; NOT a message receive HANDLER
 * callback, just a dumb callback that must be called when a message
 * is received to stop the monitor aborting.  You might pass
 * it to fgr_msg_receive_cb_set().
 *
 * @param unused unused parameter, only present so that this
                 function matches fgr_msg_receive_cb_t.
 */
void fgr_monitor_msg_receive_cb(void *unused);

/** Cause an abort: this will call the callback that was passed
 * to fgr_monitor_init() (if present), deinitialise everything
 * then call abort(), which will write a crash-dump to flash
 * and restart the system.
 *
 * @param reason     your abort reason, must be a positive number,
 *                   i.e. greater than FGR_MONITOR_ABORT_REASON_USER_LAST.
 * @param task_name  a task name to go with the abort reason;
 *                   up to FGR_UTIL_TASK_NAME_MAX_LENGTH - 1
 *                   in length with a null terminator after that;
 *                   may be NULL if there is no associated task name.
 */
void fgr_monitor_abort(uint8_t reason, const char *task_name);

/** Function to obtain the reason for a monitor abort.  You might
 * call this function at boot to determine whether the (re)boot
 * was a result of fgr_monitor triggering an abort.  The abort
 * reason is cleared when this function returns.
 *
 * See also fgr_monitor_reason_log().
 *
 * @param task_name a pointer to storage for up to
 *                  FGR_UTIL_TASK_NAME_MAX_LENGTH characters
 *                  (that includes room for a null terminator)
 *                  which will, if the abort reason is for instance
 *                  FGR_MONITOR_ABORT_REASON_TASK_LOW_STACK or
 *                  FGR_MONITOR_ABORT_REASON_TASK_WDT, be
 *                  populated with the associated task name; may
 *                  be NULL.
 * @return          the monitor abort reason, which may be one from
 *                  fgr_monitor_abort_reason_t or may be your own
 *                  abort reason if you called fgr_monitor_abort().
 */
int32_t fgr_monitor_abort_reason_get(char *task_name);

/** Like fgr_monitor_abort_reason_get() but instead logs the
 * abort reason in an ESP_LOGx() call.  The abort reason is
 * cleared when this function returns.
 *
 * @param tag    the tag to apply to the log message; may be NULL
 *               in which case whatever is the default tag for
 *               monitor messages will be employed.
 * @param prefix a prefix to put in front of the abort reason;
 *               may be NULL.
 * @param level  the log level to log the abort as.
 * @return       1 if there was an abort, ESP_OK if not, else a
 *               negative error code from esp_err_t.
 */
int32_t fgr_monitor_abort_reason_log(const char *tag, const char *prefix,
                                     esp_log_level_t level);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MONITOR_H_

// End of file
