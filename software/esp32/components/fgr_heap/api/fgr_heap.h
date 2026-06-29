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

#ifndef _FGR_HEAP_H_
#define _FGR_HEAP_H_

/** @file
 * @brief Heap checking API for a node of the front garden railway:
 * #include "fgr_heap_wrapper.h" at the end of your include files
 * and then call MALLOC(), CALLOC(), REALLOC() or FREE() instead of
 * malloc(), calloc(), realloc() and free().  Then you may include
 * this header and call the functions here to find out who has what
 * in the heap.
 */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_HEAP_CHECK_RECORDS
// The maximum number of heap allocations to track the source of.
#  define FGR_HEAP_CHECK_RECORDS 512
#endif

#ifndef FGR_HEAP_CHECK_LEAK_RECORDS
// The maximum number of heap leaks to store in retained RAM on deinit.
#  define FGR_HEAP_CHECK_LEAK_RECORDS 5
#endif

#ifndef FGR_HEAP_INTERVAL_SECONDS
// How frequently to run resolution of the heap records in seconds.
#  define FGR_HEAP_INTERVAL_SECONDS 1
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise heap checking.  This function should be the first
 * thing called at start of day if you want to track all of the
 * heap allocations made by the fgr code.
 *
 * Note: this will create a semaphore that is never destroyed.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_heap_init();

/** Deinitialise heap checking. This should be the last thing
 * called at system shut-down: it will store information on
 * any outstanding malloc()/calloc()/realloc() calls in retained
 * RAM, information which can be retrieved with the
 * fgr_heap_leak_*() functions.
 */
void fgr_heap_deinit();

/** Get the amount of heap currently allocated through malloc()/
 * calloc()/realloc() calls from the fgr code.
 *
 * @return  on success, the amount of heap memory allocated,
 *          else negative value from esp_err_t; in particular,
 *          -ESP_ERR_NOT_FOUND will be returned if it is not
 *          possible to do such a calculation because allocation
 *          records were lost (e.g. due to limited space available
 *          for storage of allocation records).
 */
int32_t fgr_heap_allocated();

/** The start of a sequence of calls to get the heap allocations
 * per file/line in the code.  Note that this ONLY covers
 * malloc()/calloc()/realloc() calls from the fgr code, it deliberately
 * does _not_ include ESP-IDF/FreeRTOS allocations that might be caused
 * by the fgr code.
 *
 * A usage pattern might be:
 *
 *    const char *file = NULL;
 *    size_t line = 0;
 *    size_t size = 0;
 *    size_t count  0;
 *    int64_t time_us = 0;
 *    buffer[32];
 *    if (fgr_heap_start(true, &file, &line, &size, &count, &time_us) >= 0) {
 *        do {
 *            buffer[0] = 0;
 *            if (count > 1) {
 *                snprintf(buffer, sizeof(buffer), " (over %d calls)", count);
 *            }
 *            printf("%s:%d allocated %d byte(s)%s @ %lld usecond(s).\n", file,
 *                   line, size, buffer, time_us);
 *        } while (fgr_heap_next(&file, &line, &size, &count, &time_us) >= 0);
 *    }
 *
 * This sequence of functions should only be called from
 * a single thread at any one time; the sequence of
 * calls is single-threaded.
 *
 * If a new heap entry is added while the call sequence is
 * it will be reported but may not be deduplicated.
 *
 * @param dedump  if true then calls from the same location will be
 *                added together and count will reflect the count,
 *                else individual calls will be reported.
 * @param file    a place to put the file being reported
 *                on; cannot be NULL.
 * @param line    a place to put the line of the file; cannot be NULL.
 * @param size    a place to put the amount of memory allocated;
 *                may be NULL.
 * @param count   a place to put the number of calls over which
 *                the amount of memory has been allocated; may
 *                be NULL (e.g. if dedump is false).
 * @param time_us place to put the time of the allocation, in
 *                microseconds since boot; if there have been
 *                multiple allocations this is the time of the
 *                first allocation.  May be NULL.
 * @return        on success, the number of calls to
 *                fgr_heap_next() that are required to
 *                read all of the heap allocations, else negative
 *                value from esp_err_t.
 */
int32_t fgr_heap_start(bool dedup, const char **file, size_t *line,
                       size_t *size, size_t *count, int64_t *time_us);

/** Get the next in the set of heap allocation values,see
 * fgr_heap_start() for an explanation.
 *
 * @param file    a place to put the file being reported
 *                on; cannot be NULL.
 * @param line    a place to put the line of the file; cannot be NULL.
 * @param size    a place to put the amount of memory allocated;
 *                may be NULL.
 * @param count   a place to put the number of calls over which
 *                the amount of memory has been allocated; may
 *                be NULL
 * @param time_us place to put the time of the allocation, in
 *                microseconds since boot; if there have been
 *                multiple allocations this is the time of the
 *                first allocation.  May be NULL.
 * @return        on success, the number of subsequent calls
 *                required to read all of the heap allocations,
 *                else negative value from esp_err_t.
 */
int32_t fgr_heap_next(const char **file, size_t *line,
                      size_t *size, size_t *count, int64_t *time_us);

/** The start of a sequence of calls to get the heap allocations
 * that remained after fgr_heap_deinit() was called, potentially in a
 * previous boot of the system.  fgr_heap_deinit() stores these in
 * order to permit leak detection.  A maximum of FGR_HEAP_CHECK_LEAK_RECORDS
 * will be reported, in descending order; the total will always be
 * the total of all leaks, even if there was no room to store them all.
 *
 * A usage pattern might be:
 *
 *    size_t total = 0;
 *    const char *file = NULL;
 *    size_t line = 0;
 *    size_t size = 0;
 *    size_t count  0;
 *    buffer[32];
 *    if (fgr_heap_leak_start(&total, &file, &line, &size, &count) >= 0) {
 *        printf("%d byte(s) leaked", total);
 *        if (total > 0) {
 *            do {
 *                buffer[0] = 0;
 *                if (count > 1) {
 *                    snprintf(buffer, sizeof(buffer), " (over %d calls)", count);
 *                }
 *                printf("%s:%d leaked %d byte(s)%s.\n", file,
 *                       line, size, buffer);
 *            } while (fgr_heap_leak_next(&file, &line, &size, &count) >= 0);
 *        }
 *    }
 *    fgr_heap_leak_stop();
 *
 * fgr_heap_leak_stop() must be called to end the sequence and
 * clear the retained RAM storage.
 *
 * This sequence of functions should only be called from
 * a single thread at any one time; the sequence of
 * calls is single-threaded.
 *
 * @param total   a place to put the total amount of memory leaked;
 *                cannot be NULL.
 * @param file    a place to put the file being reported
 *                on; cannot be NULL.
 * @param line    a place to put the line of the file; cannot be NULL.
 * @param size    a place to put the amount of memory allocated;
 *                may be NULL.  Note that this is a sum of all
 *                outstanding allocations from that file/line over
 *                time, so may be larger than the size allocated
 *                if there is more than one oustanding allocations.
 * @return        on success, the number of calls to
 *                fgr_heap_leak_next() that are required to
 *                read all of the heap allocations, else negative
 *                value from esp_err_t; in particular, as with
 *                fgr_heap_allocated(), -ESP_ERR_NOT_FOUND will
 *                be returned if it was not possible to detect a
 *                leak due to loss of messages while recording
 *                heap allocations.  -ESP_ERR_INVALID_VERSION will
 *                be returned if there was a leak but it is not
 *                valid to report it since the software has changed
 *                (and therefore file/line references will have
 *                changed since the list was stored).
 */
int32_t fgr_heap_leak_start(size_t *total, const char **file, size_t *line,
                            size_t *size);

/** Get the next in the set of leaked heap allocation values,
 * see fgr_heap_leak_start() for an explanation.
 *
 * @param file    a place to put the file being reported
 *                on; cannot be NULL.
 * @param line    a place to put the line of the file; cannot be NULL.
 * @param size    a place to put the amount of memory allocated;
 *                may be NULL.  Note that this is a sum of all
 *                outstanding allocations from that file/line over
 *                time, so may be larger than the size allocated
 *                if there is more than one oustanding allocations.
 * @return        on success, the number of subsequent calls
 *                required to read all of the heap allocations,
 *                else negative value from esp_err_t.
 */
int32_t fgr_heap_leak_next(const char **file, size_t *line,
                           size_t *size);

/** Stop a sequence of calls stated by fgr_heap_leak_start()
 * and clear the retained RAM record.
 */
void fgr_heap_leak_stop();

/** As the fgr_heap_leak_*() calls but, instead of returning the
 * data, logs it in an ESP_LOGx() macros with the given ESP-IDF
 * log level.  When this function returns the retained RAM record
 * will have been cleared.
 *
 * @param tag    the tag to apply to the log message; may be NULL
 *               in which case whatever is the default tag for
 *               heap messages will be employed.
 * @param prefix a prefix to put in front of the string of data;
 *               may be NULL.
 * @param level  the log level to log the string as; this will
 *               only be applied if there actually has been a leak,
 *               otherwise the level will be "info" for a leak of
 *               zero or "debug" if an error is reported.
 * @return       the number of bytes leaked, ESP_OK if not, else a
 *               negative error code from esp_err_t; in particular,
 *               as with fgr_heap_allocated(), -ESP_ERR_NOT_FOUND will
 *               be returned if it was not possible to detect a
 *               leak due to loss of messages while recording
 *               heap allocations and -ESP_ERR_INVALID_VERSION will
 *               be returned if there was a leak but it is not
 *               valid to report it since the software has changed
 *               (and therefore file/line references will have
 *               changed since the list was stored).
 */
int32_t fgr_heap_leak_log(const char *tag, const char *prefix,
                          esp_log_level_t level);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_MONITOR_H_

// End of file
