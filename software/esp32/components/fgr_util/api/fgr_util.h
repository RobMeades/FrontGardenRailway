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

#include "stddef.h"
#include "stdbool.h"
#include "esp_log.h"
#include "esp_debug_helpers.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#define FGR_UTIL_ARRAY_LENGTH(array) (sizeof(array) / sizeof(array[0]))

#ifndef FGR_UTIL_WATCHDOG_FEED_TIME_MS
// The number of milliseconds to vTaskDelay() for in order to let the
// idle task feed its watchdog
#  define FGR_UTIL_WATCHDOG_FEED_TIME_MS 10
#endif

// IMPORTANT: only use the macros below as "outer" braces once, with whole
// sets of open/close brace pairs between them, don't attempt to
// us them nested: since the macros contain braces themselves,
// weird things will happen.
//
// For instance, don't do this:
//
//     CONTEXT_LOCK()
//     if (thing) {
//
//         CONTEXT_UNLOCK()
//         do_a_thing_that_needs_unlocking();
//         CONTEXT_LOCK()
//
//     }
//     CONTEXT_UNLOCK()
//
// In those situations, just use xSemaphoreTake() and xSemaphoreGive()
// directly on the inner lock.
# if 0
// Lock a context.
#  define CONTEXT_LOCK(semaphore, dbg)    {                                               \
                                              printf(TAG "+SEM 0 %s\n", dbg);             \
                                              xSemaphoreTake(semaphore, portMAX_DELAY);   \
                                              printf(TAG "+SEM 1 %s\n", dbg)

// Unlock a context.
#  define CONTEXT_UNLOCK(semaphore, dbg)      printf(TAG "-SEM 1 %s\n", dbg);             \
                                              xSemaphoreGive(semaphore);                  \
                                              printf(TAG "-SEM 0 %s\n", dbg);             \
                                          }
#else
// Lock a context.
#  define CONTEXT_LOCK(semaphore, dbg)    {                                             \
                                              xSemaphoreTake(semaphore, portMAX_DELAY)

// Unlock a context.
#  define CONTEXT_UNLOCK(semaphore, dbg)      xSemaphoreGive(semaphore);                \
                                          }
#endif


#ifndef FGR_UTIL_RETAINED_RAM_MAGIC_MARKER
// Marker to store in an int32_t indicating that a retained RAM area is populated.
#  define FGR_UTIL_RETAINED_RAM_MAGIC_MARKER 0xdeadface
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** A task.
 *
 * @param param  cb_param as passed to fgr_util_task_create().
 */
typedef void (*fgr_util_task_cb_t)(void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS: STATIC
 * -------------------------------------------------------------- */

/** Check if a double-indirect is correct: use this wherever
 * a function receives a ** parameter, e.g.:
 *
 *     if (is_valid_ptr_to_ptr(context, TAG, "channel context")) {
 *        // do stuff
 *     }
 *
 * Note: defined as static in a header file so that it will be
 * inlined properly.
 *
 * @param pptr   the ** parameter to check.
 * @param tag    the TAG that would have been passed to ESP_LOGX().
 * @param name   a null-terminated string to identify the pointer
 *               being checked.
 * @return       true if the parameter passes the check, else false.
 */
static inline bool fgr_util_is_valid_ptr_to_ptr(void **pptr, const char *tag,
                                                const char *name,
                                                const char *file, int32_t line)
{
    if (pptr == NULL) {
        ESP_LOGE(tag, "Invalid %s: context is NULL", name);
        return false;
    }

    uint32_t addr = (uint32_t)pptr;
    if (addr < 0x3f000000 || addr > 0x3fffffff) {
        ESP_LOGE(tag, "Invalid %s: context=%p (not in heap range)", name, pptr);
        return false;
    }

    if (*pptr != NULL) {
        uint32_t val = (uint32_t)(*pptr);
        if (val < 0x3f000000 || val > 0x3fffffff) {
            ESP_LOGE(tag, "%s:%d: ATTENTION ATTENTION Invalid %s: *context=%p (not a valid pointer) ATTENTION ATTENTION",
                     file, line, name, *pptr);
            // Print backtrace to see who called this
            esp_backtrace_print(100);
            return false;
        }
    }

    return true;
}

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise debug.
 *
 * Note: this will create a semaphore that is never destroyed.
 *
 * @return ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_util_init();

/** Deinitialise utils.
 */
void fgr_util_deinit();

/** Create a task.  The advantage of doing your task creation this way
 * is that (a) task exit is orchestrated properly and (b) watchdog timers
 * are applied and monitored automagically.  The callback will be called
 * in a loop with FGR_UTIL_WATCHDOG_FEED_TIME_MS loop delay to let the
 * idle task feed its task watchdog.
 *
 * @param cb               the callback that forms the body of the task;
 *                         cannot be NULL.  The callback must return
 *                         in a reasonably time frame, e.g. within
 *                         10 seconds, in order that the rest of the
 *                         task loop runs to feed the watchdogs.
 * @param cb_param         a parameter to pass to the callback; may
 *                         be NULL.
 * @param name             a null-terminated string that forms the
 *                         name of the task, purely for debugging
 *                         purposes, may be truncated if more than
 *                         about 16 characters in length, may be NULL.
 *                         If non-NULL this MUST be a true constant
 *                         that is valid for the duration of the
 *                         task's run.
 * @param stack_size_bytes the amount of stack to allocate for the
 *                         task in bytes.
 * @param priority         the task priority, where higher numbers
 *                         represent a higher priority, normal
 *                         ESP-IDF task priority.
 * @param handle           a pointer to a place to store the task
 *                         handle; cannot be NULL.  The handle will
 *                         be a good 'ole FreeRTOS TaskHandle, nothing
 *                         weird; you can do anything with it that
 *                         you could do with a TaskHandle.  Will not
 *                         be touched on failure.
 * @return                 ESP_OK on success, else a negative value
 *                         from esp_err_t.
 */
int32_t fgr_util_task_create(fgr_util_task_cb_t cb,
                             void *cb_param,
                             const char *name,
                             size_t stack_size_bytes,
                             int32_t priority,
                             void *handle);

/** Determine if the given task is running.
 *
 * @param handle  the handle of the task.
 * @return        true if the task is running, else false.
 */
bool fgr_util_task_is_running(void *handle);

/** Get the stack high watermark, i.e. the smallest
 * amount of stack ever left over time.
 *
 * @param handle  the handle of the task.
 * @return        the high watermark in bytes,
 *                zero if handle is not found.
 */
int32_t fgr_util_min_free_stack(void *handle);

/** The start of a sequence of calls to get the minimum
 * free stack values of all of the tasks that fgr_utils
 * knows about: i.e. fgr_util_min_free_stack() for all.
 *
 * A usage pattern might be:
 *
 *    const char *name = NULL;
 *    int32_t min_free = 0;
 *    if (fgr_util_min_free_stack_start(&name, &min_free) >= 0) {
 *        do {
 *            printf("task %s had min free stack %d.\n", name, min_free);
 *        } while (fgr_util_min_free_stack_next(&name, &min_free) >= 0);
 *        fgr_util_min_free_stack_stop();
 *    }
 *
 * When fgr_util_min_free_stack_start() has returned a
 * non-negative value and you don't loop through all of the
 * entries until fgr_util_min_free_stack_next() returns zero
 * or less, fgr_util_min_free_stack_stop() MUST be
 * called to terminate the sequence (and there is no
 * harm in always calling it at the end of the sequence
 * even if you do loop through the lot).
 *
 * This sequence of functions should only be called from
 * a single thread at any one time; the sequence of
 * calls is single-threaded.
 *
 * If a new task is added with a call to fgr_util_task_create()
 * it may or may not be included; best not do that.
 *
 * @param name           a place to put the name of the task
 *                       being reported on; cannot be NULL.
 * @param min_free_bytes a place to put the minimum free stack
 *                       value (in bytes); cannot be NULL.
 * @return               on success, the number of calls to
 *                       fgr_util_min_free_stack_next() that
 *                       are required to read all of the
 *                       task stack free min values, else
 *                       negative value from esp_err_t.
 */
int32_t fgr_util_min_free_stack_start(const char **name,
                                      int32_t *min_free);

/** Get the next in the set of minimum free stack values,
 * see fgr_util_min_free_stack_start() for an explanation.
 *
 * @param name           a place to put the name of the task
 *                       being reported on; cannot be NULL.
 * @param min_free_bytes a place to put the minimum free stack
 *                       value (in bytes); cannot be NULL.
 * @return               on success, the number of subsequent
 *                       calls required to read all of the
 *                       task stack free min values, else
 *                       negative value from esp_err_t.
 */
int32_t fgr_util_min_free_stack_next(const char **name,
                                     int32_t *min_free);

/** Stop reading task stack high watermark values, see
 * fgr_util_min_free_stack_start() for an explanation.
 */
void fgr_util_min_free_stack_stop();

/** Destroy a task.
 *
 * @param handle  the handle of the task to destroy.
 */
void fgr_util_task_destroy(void *handle);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_UTIL_H_

// End of file
