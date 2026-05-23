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

// Get the number of elements in an array.
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
                                              printf(TAG "+SEM 0 %s\n", dbg ? dbg : "");  \
                                              xSemaphoreTake(semaphore, portMAX_DELAY);   \
                                              printf(TAG "+SEM 1 %s\n", dbg ? dbg : "")

// Unlock a context.
#  define CONTEXT_UNLOCK(semaphore, dbg)      printf(TAG "-SEM 1 %s\n", dbg ? dbg : "");    \
                                              xSemaphoreGive(semaphore);                    \
                                              printf(TAG "-SEM 0 %s\n", dbg ? dbg : "");    \
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

#ifndef FGR_UTIL_TASK_NAME_MAX_LENGTH
// The maximum length of a task name, used when stack overflows or task
// watchdog timeouts occur.  Includes room for a null terminator.
#  define FGR_UTIL_TASK_NAME_MAX_LENGTH (16 + 1)
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

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
 * FUNCTIONS: MISC
 * -------------------------------------------------------------- */

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_UTIL_H_

// End of file
