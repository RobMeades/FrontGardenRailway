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

/** @file
 * @brief Heap checking for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "esp_log.h"
#include "sys/queue.h"
#include "stdatomic.h"

#include "fgr_util.h"
#include "fgr_rram.h"
#include "fgr_ota.h"

#include "fgr_heap.h"

// Must be last in the inclusions to poison calls to malloc()/free()
#include "fgr_heap_wrapper.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "heap"

#ifndef FGR_HEAP_TASK_STACK_SIZE
#  define FGR_HEAP_TASK_STACK_SIZE (1024 * 4)
#endif

#ifndef FGR_HEAP_QUEUE_LENGTH
// Seems a bit extreme I know, but they can arrive quite rapidly.
#  define FGR_HEAP_QUEUE_LENGTH FGR_HEAP_CHECK_RECORDS
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Structure that defines a heap operation.
typedef struct {
    const char *path; // NULL indicates that the heap task should exit
    int line;
    void *address;
    size_t size;  // 0 for a FREE()
    int64_t time_us;
} heap_operation_t;

// Structure that defines a heap operation, designed to be
// used as part of a linked list
typedef struct heap_record_t {
    heap_operation_t operation;
    bool reported;
    SLIST_ENTRY(heap_record_t) next;
} heap_record_t;

// Heap list head.
SLIST_HEAD(heap_record_list_t, heap_record_t);

// Context for the heap checking task.
typedef struct {
    SemaphoreHandle_t lock;
    struct heap_record_list_t heap_record_list;
} context_task_t;

// Top-level context.
typedef struct {
    SemaphoreHandle_t lock;
    bool is_shutting_down;
    fgr_util_cb_t cb;
    void *cb_param;
    TaskHandle_t handle;
    bool running;
    SemaphoreHandle_t running_semaphore;
    QueueHandle_t queue_handle;
    bool dedup;
    context_task_t context_task;
    size_t leak_list_next;
    atomic_uint_least32_t allocated;
    atomic_uint_least32_t record_count;
    atomic_uint_least32_t record_lost_count;
} context_t;

// Structure to hold leaked allocations in retained RAM
// at end of day.
typedef struct {
    const char *path;
    int line;
    size_t size;
} heap_leak_t;

// Structure to hold a list of leaked allocations in
// retained RAM at end of day.
typedef struct {
    int32_t allocated_or_error;
    heap_leak_t allocation[FGR_HEAP_CHECK_LEAK_RECORDS];
    int8_t count;
    uint8_t image_sha256[32];
} heap_leak_list_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static context_t g_context = {0};

// Storage for the memory leaks in retained RAM.
FGR_RRAM_DEFINE(heap_leak_list_t, leak_list);

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Send a heap operation to the heap task.
// Note: don't do any locking of anything in here, it is
// in the malloc()/free() path.
static void heap_task_send(const char *path, int line, void *address, size_t size)
{
    if (g_context.queue_handle) {
        heap_operation_t operation = {
            .path = path,
            .line = line,
            .address= address,
            .size = size,
            .time_us = esp_timer_get_time()
        };

        if (xQueueSend(g_context.queue_handle, &operation, 0) != pdPASS) {
            atomic_fetch_add(&g_context.record_lost_count, 1);
        }
    }
}


// De-duplicate the current list, called by resolve_allocations()
// IMPORTANT: the list must be locked before this is called.
static void deduplicate_allocations(struct heap_record_list_t *list)
{
    if (!SLIST_EMPTY(list)) {
        struct heap_record_t *current;
        struct heap_record_t *runner;
        struct heap_record_t *prev;
        struct heap_record_t *next;

        current = SLIST_FIRST(list);

        // For each element in the list
        while (current != NULL) {
            prev = current;
            runner = SLIST_NEXT(current, next);

            // Check all subsequent elements
            while (runner != NULL) {
                next = SLIST_NEXT(runner, next);

                // If same path pointer and line, combine sizes and remove runner
                if (current->operation.path == runner->operation.path &&
                    current->operation.line == runner->operation.line) {

                    // Add the size to the current entry
                    current->operation.size += runner->operation.size;

                    // Remove runner from the list
                    SLIST_REMOVE_AFTER(prev, next);

                    // Free the removed node
                    FGR_HEAP_REAL_FREE(runner);

                    // Update runner to the next element
                    runner = next;
                } else {
                    // Move to next element
                    prev = runner;
                    runner = next;
                }
            }

            // Move to next unique element
            current = SLIST_NEXT(current, next);
        }
    }
}

// Sort the record list into descending order, called by resolve_allocations()
// IMPORTANT: the list must be locked before this is called.
static void sort_allocations(struct heap_record_list_t *list)
{
    if (!SLIST_EMPTY(list)) {
        struct heap_record_list_t sorted_list;
        SLIST_INIT(&sorted_list);

        // Move all elements, inserting in sorted order
        while (!SLIST_EMPTY(list)) {
            heap_record_t *rec = SLIST_FIRST(list);
            SLIST_REMOVE_HEAD(list, next);

            // Insert rec into sorted_list (descending, largest first)
            if (SLIST_EMPTY(&sorted_list)) {
                SLIST_INSERT_HEAD(&sorted_list, rec, next);
            } else {
                heap_record_t *current = SLIST_FIRST(&sorted_list);
                heap_record_t *prev = NULL;

                while ((current != NULL) && (current->operation.size < rec->operation.size)) {
                    prev = current;
                    current = SLIST_NEXT(current, next);
                }

                if (prev == NULL) {
                    SLIST_INSERT_HEAD(&sorted_list, rec, next);
                } else {
                    SLIST_INSERT_AFTER(prev, rec, next);
                }
            }
        }

        // Move all elements back (they'll be in reverse order, but we
        // want descending order, so this is correct)
        while (!SLIST_EMPTY(&sorted_list)) {
            heap_record_t *rec = SLIST_FIRST(&sorted_list);
            SLIST_REMOVE_HEAD(&sorted_list, next);
            SLIST_INSERT_HEAD(list, rec, next);
        }
    }
}

// Resolve the current stored allocations.
// IMPORTANT: the list must be locked before this is called.
static void resolve_allocations(context_t *context)
{
    context_task_t *context_task = (context_task_t *)&context->context_task;
    bool restart;
    size_t allocated = 0;

    do {
        restart = false;
        heap_record_t *iter_outer;

        SLIST_FOREACH(iter_outer, &context_task->heap_record_list, next) {
            if (iter_outer->operation.size == 0) {
                // Found a free() record
                heap_record_t *iter_inner;
                heap_record_t *prev_inner = NULL;
                bool found_malloc = false;

                // Search for matching allocation
                SLIST_FOREACH(iter_inner, &context_task->heap_record_list, next) {
                    if (iter_inner != iter_outer &&
                        iter_inner->operation.size > 0 &&
                        iter_inner->operation.address == iter_outer->operation.address) {
                        found_malloc = true;
                        break;
                    }
                    prev_inner = iter_inner;
                }

                if (found_malloc) {
                    // Remove and free the allocation record
                    if (prev_inner == NULL) {
                        SLIST_REMOVE_HEAD(&context_task->heap_record_list, next);
                    } else {
                        SLIST_REMOVE_AFTER(prev_inner, next);
                    }
                    FGR_HEAP_REAL_FREE(iter_inner);
                    atomic_fetch_sub(&context->record_count, 1);
                }

                // Remove and free the free() record
                heap_record_t *scan_prev = NULL;
                heap_record_t *scan = SLIST_FIRST(&context_task->heap_record_list);
                while (scan != NULL && scan != iter_outer) {
                    scan_prev = scan;
                    scan = SLIST_NEXT(scan, next);
                }

                if (scan == iter_outer) {
                    if (scan_prev == NULL) {
                        SLIST_REMOVE_HEAD(&context_task->heap_record_list, next);
                    } else {
                        SLIST_REMOVE_AFTER(scan_prev, next);
                    }
                    FGR_HEAP_REAL_FREE(iter_outer);
                    atomic_fetch_sub(&context->record_count, 1);
                    restart = true;
                }

                break;
            } else {
                allocated += iter_outer->operation.size;
            }
        }
    } while (restart);

    atomic_store(&context->allocated, allocated);
}

// Store the current outstanding heap allocations in retained RAM.
// IMPORTANT: the list must be locked before this is called.
static void store_leak_list(context_t *context)
{
    context_task_t *context_task = (context_task_t *) &context->context_task;
    heap_leak_list_t leak_list = {0};

    leak_list.allocated_or_error = fgr_ota_current_sha256(leak_list.image_sha256);
    if (leak_list.allocated_or_error == ESP_OK) {
        leak_list.allocated_or_error = -ESP_ERR_NOT_FOUND;
        if (atomic_load(&context->record_lost_count) == 0) {

            // Resolve the current set of allocations, deduplicate
            // them by file/line and then sort them
            resolve_allocations(context);
            deduplicate_allocations(&context_task->heap_record_list);
            sort_allocations(&context_task->heap_record_list);

            // Populate the shadow variable
            leak_list.allocated_or_error = atomic_load(&context->allocated);
            heap_leak_t *allocation = leak_list.allocation;
            int32_t stored_count = 0;

            heap_record_t *iter;
            SLIST_FOREACH(iter, &context_task->heap_record_list, next) {
                if (iter->operation.size > 0) {  // Only store actual allocations
                    allocation->path = iter->operation.path;
                    allocation->line = iter->operation.line;
                    allocation->size = iter->operation.size;
                    allocation++;
                    stored_count++;
                    if (stored_count >= FGR_UTIL_ARRAY_LENGTH(leak_list.allocation)) {
                        break;
                    }
                }
            }
            leak_list.count = stored_count;
        }
    }

    // Set the value in retained RAM
    FGR_RRAM_SET(leak_list);
}

// Clean up.
static void clean_up(context_t *context)
{
    if (context->lock) {

        CONTEXT_LOCK(context->lock, "clean_up() heap");

        // Tell the heap task to end in an orderly manner,
        // eating up everying on the queue and resolving
        // any remaining free()s, by sending it a NULL path
        heap_task_send(NULL, 0, NULL, 0);
        // Take the running semaphore to know its stopped
        CONTEXT_LOCK(context->running_semaphore, "clean_up() heap task 1");
        CONTEXT_UNLOCK(context->running_semaphore, "clean_up() heap task 1");
        vSemaphoreDelete(context->running_semaphore);

        if (g_context.queue_handle) {
            vQueueDelete(g_context.queue_handle);
            g_context.queue_handle = NULL;
        }

        context_task_t *context_task = &context->context_task;
        if (context_task->lock) {

            CONTEXT_LOCK(context_task->lock, "clean_up() heap task 2");

            // If we've been called from deinit, store the leak list
            if (context->is_shutting_down) {
                store_leak_list(context);
            }

            while (!SLIST_EMPTY(&context_task->heap_record_list)) {
                heap_record_t*p = SLIST_FIRST(&context_task->heap_record_list);
                SLIST_REMOVE_HEAD(&context_task->heap_record_list, next);
                FGR_HEAP_REAL_FREE(p);
            }

            CONTEXT_UNLOCK(context_task->lock, "clean_up() heap task 2");
            vSemaphoreDelete(context_task->lock);

            atomic_store(&g_context.record_count, 0);
            atomic_store(&g_context.record_lost_count, 0);
        }

        CONTEXT_UNLOCK(context->lock, "clean_up() heap");
        // The semaphore will be re-used
    }
}

// Return the file name portion of a path.
static const char *file_name_from_path(const char * path)
{
    const char * file_name = path;

    // Find the last occurrence of '/'
    const char *last_slash = strrchr(path, '/');
    if (last_slash) {
        file_name = last_slash + 1;
    }

    return file_name;
}

// Heap tracking task.
static void task_heap(void *param)
{
    context_t *context = (context_t *) param;
    context_task_t *context_task = (context_task_t *) &context->context_task;
    heap_operation_t operation;
    int64_t last_run_us = esp_timer_get_time();

    esp_task_wdt_add(NULL);

    CONTEXT_LOCK(context->running_semaphore, "task_heap() running");

    while (context->running) {

        CONTEXT_LOCK(context_task->lock, "task_heap()");

        // Collect any new heap operations
        while (context->running &&
               (xQueueReceive(context->queue_handle, &operation, 0) == pdTRUE)) {
            if (operation.path != NULL) {
                // Debug: track malloc count
                if (atomic_load(&context->record_count) < FGR_HEAP_CHECK_RECORDS) {
                    heap_record_t *record = FGR_HEAP_REAL_MALLOC(sizeof(*record));
                    if (record) {
                        memset(record, 0, sizeof(*record));
                        record->operation = operation;
                        record->operation.path = file_name_from_path(operation.path);
                        SLIST_INSERT_HEAD(&context_task->heap_record_list, record, next);
                        atomic_fetch_add(&context->record_count, 1);
                    } else {
                        atomic_fetch_add(&context->record_lost_count, 1);
                    }
                } else {
                    atomic_fetch_add(&context->record_lost_count, 1);
                }
            } else {
                // A NULL path value means exit
                context->running = false;
                ESP_LOGI(TAG, "task_heap received exit signal.");
            }
        }

        if (!context->running || (esp_timer_get_time() - last_run_us > FGR_HEAP_INTERVAL_SECONDS * 1000000)) {
            resolve_allocations(context);
        }

        CONTEXT_UNLOCK(context_task->lock, "task_heap()");

        esp_task_wdt_reset();
        last_run_us = esp_timer_get_time();
        if (context->running) {
            vTaskDelay(pdMS_TO_TICKS(FGR_HEAP_INTERVAL_SECONDS * 1000));
        }
    }

    ESP_LOGI(TAG, "task_heap exiting.");
    vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));

    CONTEXT_UNLOCK(context->running_semaphore, "task_heap() running");

    esp_task_wdt_delete(NULL);
    vTaskDelete(NULL);
}

// Report the first unreported file/line group.
// Returns the number of records still unreported.
// IMPORTANT: the context should be locked before this is called.
int32_t heap_report_next(struct heap_record_list_t *heap_record_list,
                         bool dedup, const char **file, size_t *line,
                         size_t *size, size_t *count, int64_t *time_us)
{
    int32_t remaining = 0;
    size_t this_count = 0;
    heap_record_t *iter;
    *file = 0;
    *line = 0;
    if (size) {
        *size = 0;
    }
    SLIST_FOREACH(iter, heap_record_list, next) {
        if (!iter->reported) {
            if ((!(*file) || (dedup && (*file == iter->operation.path))) &&
                (!(*line) || (dedup && (*line == iter->operation.line)))) {
                *file = iter->operation.path;
                *line = iter->operation.line;
                this_count++;
                if (size) {
                    *size += iter->operation.size;
                }
                if (count) {
                    *count = this_count;
                }
                if (time_us && (this_count == 1)) {
                    *time_us = iter->operation.time_us;
                }
                iter->reported = true;
            } else {
                remaining++;
            }
        }
    }

    return remaining;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: GENERAL
 * -------------------------------------------------------------- */

// Initialise heap checking.
int32_t fgr_heap_init()
{
    int32_t err = ESP_OK;
    context_task_t *context_task = &g_context.context_task;

    if (!g_context.lock) {
        // Create mutex
        err = -ESP_ERR_NO_MEM;
        g_context.lock = xSemaphoreCreateMutex();
        SLIST_INIT(&context_task->heap_record_list);
    }

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_heap_init()");

        g_context.is_shutting_down = false;

        if (!g_context.queue_handle) {
            g_context.queue_handle = xQueueCreate(FGR_HEAP_QUEUE_LENGTH, sizeof(heap_operation_t));
        }

        if (g_context.queue_handle) {
            // Note: we don't use fgr_task_create() here since
            // the heap task needs to live on beyond
            // fgr_task_deinit()
            g_context.running_semaphore = xSemaphoreCreateMutex();
            context_task->lock = xSemaphoreCreateMutex();
            if (g_context.running_semaphore && context_task->lock) {
                g_context.running = true;
                if (xTaskCreate(task_heap, TAG, FGR_HEAP_TASK_STACK_SIZE,
                                &g_context, 2, &g_context.handle) == pdPASS) {
                    err = ESP_OK;
                } else {
                    g_context.running = false;
                    g_context.handle = NULL;    // Just in case
                }
            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_heap_init()");
    }

    if (err != ESP_OK) {
        clean_up(&g_context);
    }

    return err;
}

// Deinitialise heap checking.
void fgr_heap_deinit()
{
    // Set this flag so that task_heap()
    // doesn't get stuck because that task
    // is in the middle of calling one of
    // this API's functions.
    g_context.is_shutting_down = true;
    clean_up(&g_context);
}

// Get the amount of heap memory currently allocated.
int32_t fgr_heap_allocated()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock && !g_context.is_shutting_down) {

        CONTEXT_LOCK(g_context.lock, "fgr_heap_allocated()");

        err = -ESP_ERR_NOT_FOUND;
        if (atomic_load(&g_context.record_lost_count) == 0) {
            err = (int32_t) atomic_load(&g_context.allocated);
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_heap_allocated()");
    }

    return err;
}

// Start a sequence of calls to get the heap allocations.
int32_t fgr_heap_start(bool dedup, const char **file, size_t *line,
                       size_t *size, size_t *count, int64_t *time_us)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    context_task_t *context_task = &g_context.context_task;

    if (file && line) {

        err = -ESP_ERR_INVALID_STATE;

        if (context_task->lock && !g_context.is_shutting_down) {

            CONTEXT_LOCK(context_task->lock, "fgr_heap_start()");

            heap_record_t *iter;
            // Reset the "reported" flag
            SLIST_FOREACH(iter, &context_task->heap_record_list, next) {
                iter->reported = false;
            }

            g_context.dedup = dedup;
            err = heap_report_next(&context_task->heap_record_list,
                                   dedup, file, line, size, count, time_us);

            CONTEXT_UNLOCK(context_task->lock, "fgr_heap_start()");
        }
    }

    return err;
}

// Get the next in the set of heap allocation values.
int32_t fgr_heap_next(const char **file, size_t *line,
                      size_t *size, size_t *count, int64_t *time_us)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    context_task_t *context_task = &g_context.context_task;

    if (file && line) {

        err = -ESP_ERR_INVALID_STATE;

        if (context_task->lock && !g_context.is_shutting_down) {

            CONTEXT_LOCK(context_task->lock, "fgr_heap_next()");

            err = heap_report_next(&context_task->heap_record_list,
                                   g_context.dedup, file, line, size,
                                   count, time_us);

            CONTEXT_UNLOCK(context_task->lock, "fgr_heap_next()");
        }
    }

    return err;
}

// The start of a sequence of calls to get leaked heap allocations.
int32_t fgr_heap_leak_start(size_t *total,
                            const char **file, size_t *line,
                            size_t *size)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (total && file && line) {
        err = -ESP_ERR_INVALID_STATE;

        if (g_context.lock && !g_context.is_shutting_down) {

            CONTEXT_LOCK(g_context.lock, "fgr_heap_leak_start()");

            heap_leak_list_t leak_list = {0};
            err = FGR_RRAM_GET(leak_list);
            if (err == ESP_OK) {
                g_context.leak_list_next = 0;
                err = leak_list.allocated_or_error;
                if (err > 0) {
                    uint8_t buffer[32];
                    err = fgr_ota_current_sha256(buffer);
                    if (err == ESP_OK) {
                        if (memcmp(buffer, leak_list.image_sha256, sizeof(leak_list.image_sha256)) == 0) {
                            *total = err;
                            err = leak_list.count;
                            if (leak_list.count > 0) {
                                *file = leak_list.allocation[0].path;
                                *line = leak_list.allocation[0].line;
                                if (size) {
                                    *size = leak_list.allocation[0].size;
                                }
                                err--;
                                g_context.leak_list_next++;
                            }
                        } else {
                            err = -ESP_ERR_INVALID_VERSION;
                        }
                    }
                }
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_heap_leak_start()");
        }
    }

    return err;
}

// Get the next in the set of leaked heap allocation values,
int32_t fgr_heap_leak_next(const char **file, size_t *line,
                           size_t *size)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (file && line) {
        err = -ESP_ERR_INVALID_STATE;

        if (g_context.lock && !g_context.is_shutting_down) {

            CONTEXT_LOCK(g_context.lock, "fgr_heap_leak_next()");

            heap_leak_list_t leak_list = {0};
            if ((g_context.leak_list_next > 0) && (g_context.leak_list_next < FGR_UTIL_ARRAY_LENGTH(leak_list.allocation))) {
                err = FGR_RRAM_GET(leak_list);
                if (err == ESP_OK) {
                    *file = leak_list.allocation[g_context.leak_list_next].path;
                    *line = leak_list.allocation[g_context.leak_list_next].line;
                    if (size) {
                        *size = leak_list.allocation[g_context.leak_list_next].size;
                    }
                    g_context.leak_list_next++;
                    err = leak_list.count - g_context.leak_list_next;
                }
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_heap_leak_next()");
        }
    }

    return err;
}

// Stop the fgr_heap_leak_*() sequence and clear retained RAM.
void fgr_heap_leak_stop()
{
    heap_leak_list_t leak_list = {0};
    FGR_RRAM_CLEAR(leak_list);
}

// Log any heap leaks.
int32_t fgr_heap_leak_log(const char *tag, const char *prefix,
                          esp_log_level_t level)
{
    char buffer[256] = "None";
    size_t total = 0;
    const char *file = NULL;
    size_t line = 0;
    size_t size = 0;
    int32_t err = fgr_heap_leak_start(&total, &file, &line, &size);

    int32_t written = 0;
    if (err < 0) {
        written = snprintf(buffer, sizeof(buffer), "%s", esp_err_to_name(-err));
    } else {
        if (total > 0){
            int32_t offset = 0;
            written = snprintf(buffer, sizeof(buffer), "%d,", total);
            if (written > 0) {
                offset += written;
            }
            while ((err >= 0) && (written >= 0) && (offset < sizeof(buffer) - 1)) {
                written = snprintf(buffer + offset, sizeof(buffer) - offset,
                                   " %s:%d (%d)", file, line, size);
                if (written > 0) {
                    offset += written;
                }
                err = fgr_heap_leak_next(&file, &line, &size);
            }
        }
        err = total;
    }
    fgr_heap_leak_stop();

    if (level > ESP_LOG_NONE) {
        if (err < 0) {
            // Override to DEBUG it there is just an error
            level = ESP_LOG_DEBUG;
        } else if (err == 0) {
            // Override to INFO it there is no leak
            level = ESP_LOG_INFO;
        }
        if (!tag) {
            tag = TAG;
        }
        if (!prefix) {
            prefix = "";
        }
        switch (level) {
            case ESP_LOG_ERROR:
                ESP_LOGE(tag, "%s%s", prefix, buffer);
                break;
            case ESP_LOG_WARN:
                ESP_LOGW(tag, "%s%s", prefix, buffer);
                break;
            case ESP_LOG_INFO:
                ESP_LOGI(tag, "%s%s", prefix, buffer);
                break;
            case ESP_LOG_DEBUG:
                ESP_LOGD(tag, "%s%s", prefix, buffer);
                break;
            case ESP_LOG_VERBOSE:
                ESP_LOGD(tag, "%s%s", prefix, buffer);
            default:
                break;
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: MALLOC/FREE
 * -------------------------------------------------------------- */

// fgr_heap version of malloc().
void *fgr_heap_malloc(size_t size, const char *path, int line)
{
    void *address = FGR_HEAP_REAL_MALLOC(size);
    heap_task_send(path, line, address, size);
    return address;
}

// fgr_heap version of calloc().
void *fgr_heap_calloc(size_t n, size_t size, const char *path, int line)
{
    void *address = FGR_HEAP_REAL_CALLOC(n, size);
    heap_task_send(path, line, address, size * n);
    return address;
}

// fgr_heap version of realloc().
void *fgr_heap_realloc(void *ptr, size_t size, const char *path, int line)
{
    if (ptr) {
        // Record the old pointer being freed
        heap_task_send(path, line, ptr, 0);
    }

    void *address = FGR_HEAP_REAL_REALLOC(ptr, size);

    if (address) {
        // Record the new allocation
        heap_task_send(path, line, address, size);
    }
    return address;
}

// fgr_heap version of free().
void fgr_heap_free(void *ptr, const char *path, int line)
{
    heap_task_send(path, line, ptr, 0);
    FGR_HEAP_REAL_FREE(ptr);
}

// End of file
