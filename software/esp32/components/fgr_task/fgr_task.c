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
 * @brief Task related functions for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_log.h"
#include "sys/queue.h"

#include "fgr_util.h"
#include "fgr_task.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "task"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Structure that defines all the things we need for a task,
// designed to be used as part of a linked list.
typedef struct task_t {
    fgr_task_cb_t cb;
    void *cb_param;
    fgr_task_state_cb_t state_cb;
    void *state_cb_param;
    const char *name;
    TaskHandle_t handle;
    bool running;
    SemaphoreHandle_t running_semaphore;
    int32_t min_free_stack_bytes;
    SLIST_ENTRY(task_t) next;
} task_t;

// Message receive callback list head.
SLIST_HEAD(task_list_t, task_t);

// Context.
typedef struct {
    SemaphoreHandle_t lock;
    task_t *min_free_stack_next_task;
    struct task_list_t task_list;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static context_t g_context = {0};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// The base task that runs all of the tasks.
static void task_base(void *param)
{
    task_t *task = (task_t *) param;
    fgr_task_state_cb_t state_cb = task->state_cb;

    esp_task_wdt_add(NULL);

    CONTEXT_LOCK(task->running_semaphore, task->name);

    while (task->running) {

        // On ESP-IDF uxTaskGetStackHighWaterMark() returns the
        // minimum free stack available in bytes, not words.
        task->min_free_stack_bytes = uxTaskGetStackHighWaterMark(NULL);

        // Do the thang
        task->cb(task->cb_param);

        // If there is a task state callback,
        // pass it an initial "STARTED" indication,
        // or otherwise a "RUNNING" indication
        if (task->state_cb) {
            fgr_task_state_t state = FGR_TASK_STATE_RUNNING;
            if (state_cb != task->state_cb) {
                state = FGR_TASK_STATE_STARTED;
            }
            task->state_cb(state, task->handle,
                           task->state_cb_param);
        }
        state_cb = task->state_cb;

        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
    }

    ESP_LOGI(TAG, "task \"%s\" exiting.", task->name);

    if (task->state_cb) {
        task->state_cb(FGR_TASK_STATE_STOPPED,
                       task->handle, task->state_cb_param);
    }

    CONTEXT_UNLOCK(task->running_semaphore, task->name);

    esp_task_wdt_delete(NULL);
    vTaskDelete(NULL);
}

// Destroy a task and remove it from the linked list.
// IMPORTANT: the context must be locked before this is called.
static void task_destroy(task_t *task)
{
    if (task) {
        // Flag should stop task running
        task->running = false;
        if (task->running_semaphore) {
            // Take the running semaphore to know its stopped
            CONTEXT_LOCK(task->running_semaphore, "task_destroy()");
            CONTEXT_UNLOCK(task->running_semaphore, "task_destroy()");
            vSemaphoreDelete(task->running_semaphore);
        }
        // Remove the task from the list, if present
        task_t *iter;
        task_t *prev = NULL;
        SLIST_FOREACH(iter, &g_context.task_list, next) {
            if (iter->handle == task->handle) {
                if (prev == NULL) {
                    // Removing the first element
                    SLIST_REMOVE_HEAD(&g_context.task_list, next);
                } else {
                    // Removing a middle element
                    SLIST_REMOVE_AFTER(prev, next);
                }
                // Done; MUST break after an insertion or removal as
                // otherwise SLIST_FOREACH will go bang as it
                // relies on pointers still being valid.
                break;
            }
            prev = iter;
        }
        free(task);
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: INITIALISATION/DEINITIALISATION
 * -------------------------------------------------------------- */

// Initialise task stuff.
int32_t fgr_task_init()
{
    int32_t err = ESP_OK;

    if (!g_context.lock) {
        // Create mutex
        err = -ESP_ERR_NO_MEM;
        g_context.lock = xSemaphoreCreateMutex();
        SLIST_INIT(&g_context.task_list);
    }

   if (g_context.lock) {
       err = ESP_OK;
   }

    return err;
}

// Deinitialise task stuff.
void fgr_task_deinit()
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_task_deinit()");

        while (!SLIST_EMPTY(&g_context.task_list)) {
            task_t *p = SLIST_FIRST(&g_context.task_list);
            task_destroy(p);
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_task_deinit()");
        // The semaphore will be re-used
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: CREATE/DESTROY/STATE
 * -------------------------------------------------------------- */

// Create a task.
int32_t fgr_task_create(fgr_task_cb_t cb,
                        void *cb_param,
                        const char *name,
                        size_t stack_size_bytes,
                        int32_t priority,
                        void *handle)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (cb && handle) {

        err = -ESP_ERR_INVALID_STATE;

        if (g_context.lock) {

            CONTEXT_LOCK(g_context.lock, "fgr_task_create()");

            err = -ESP_ERR_NO_MEM;
            task_t *task = (task_t *) malloc(sizeof(*task ));
            if (task) {
                memset(task, 0, sizeof(*task));
                task->cb = cb;
                task->cb_param = cb_param;
                task->name = name;
                task->running_semaphore = xSemaphoreCreateMutex();
            }
            if (task && task->running_semaphore) {
                task->running = true;
                if (xTaskCreate(task_base, name, stack_size_bytes,
                                task, priority, &task->handle) == pdPASS) {
                    *((TaskHandle_t *) handle) = task->handle;
                    SLIST_INSERT_HEAD(&g_context.task_list, task, next);
                    err = ESP_OK;
                } else {
                    task->running = false;
                    task->handle = NULL;    // Just in case
                }
            }

            if (err != ESP_OK) {
                task_destroy(task);
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_task_create()");
        }
    }

    return err;
}

// Destroy a task.
void fgr_task_destroy(void *handle)
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_task_destroy()");

        task_t *iter;
        SLIST_FOREACH(iter, &g_context.task_list, next) {
            if (iter->handle == handle) {
                ESP_LOGD(TAG, "task \"%s\" high watermark was %d byte(s).",
                         iter->name ? iter->name : "",
                         iter->min_free_stack_bytes);
                task_destroy(iter);
                break;
            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_task_destroy()");
    }
}

// Determine if the given task is running.
bool fgr_task_is_running(void *handle)
{
    bool task_is_running = false;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_task_is_running()");

        task_t *iter;
        SLIST_FOREACH(iter, &g_context.task_list, next) {
            if (iter->handle == handle) {
                task_is_running = true;
                break;
            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_task_is_running()");
    }

    return task_is_running;
}

// Set a callback that will be called every task loop.
int32_t fgr_task_state_cb(void *handle,
                          fgr_task_state_cb_t cb,
                          void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (handle) {
        err = -ESP_ERR_INVALID_STATE;

        if (g_context.lock) {

            CONTEXT_LOCK(g_context.lock, "fgr_state_cb()");

            task_t *iter;
            SLIST_FOREACH(iter, &g_context.task_list, next) {
                if (iter->handle == handle) {
                    iter->state_cb = cb;
                    iter->state_cb_param = cb_param;
                    break;
                }
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_task_state_cb()");
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: STACK RELATED
 * -------------------------------------------------------------- */

// Get the stack high watermark.
int32_t fgr_task_min_free_stack(void *handle)
{
    int32_t high_watermark = 0;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_task_min_free_stack()");

        task_t *iter;
        SLIST_FOREACH(iter, &g_context.task_list, next) {
            if (iter->handle == handle) {
                high_watermark = iter->min_free_stack_bytes;
                break;
            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_task_min_free_stack()");
    }

    return high_watermark;
}

// The start the sequence of calls to get the minimum free stack.
int32_t fgr_task_min_free_stack_start(const char **name,
                                      int32_t *min_free)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (name && min_free) {

        err = -ESP_ERR_INVALID_STATE;

        if (g_context.lock) {

            CONTEXT_LOCK(g_context.lock, "fgr_task_min_free_stack_start()");

            err = -ESP_ERR_NOT_FOUND;

            if (!g_context.min_free_stack_next_task) {
                task_t *iter;
                err = 0;
                SLIST_FOREACH(iter, &g_context.task_list, next) {
                    if (err == 0) {
                        *name = iter->name;
                        *min_free = iter->min_free_stack_bytes;
                    } else if (err == 1) {
                        g_context.min_free_stack_next_task = iter;
                    }
                    err++;
                }

                // Return the number of tasks _remaining_ in the list,
                // will return ESP_FAIL (-1) if there were no tasks at all
                err--;
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_task_min_free_stack_start()");
        }
    }

    return err;
}

// Get the next in the set of minimum free stack values.
int32_t fgr_task_min_free_stack_next(const char **name,
                                     int32_t *min_free)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (name && min_free) {

        err = -ESP_ERR_INVALID_STATE;

        if (g_context.lock) {

            CONTEXT_LOCK(g_context.lock, "fgr_task_min_free_stack_next()");

            err = -ESP_ERR_NOT_FOUND;

            if (g_context.min_free_stack_next_task) {
                task_t *iter;
                err = 0;
                SLIST_FOREACH(iter, &g_context.task_list, next) {
                    if (err > 0) {
                        if (err == 1) {
                            g_context.min_free_stack_next_task = iter;
                        }
                        // If we are beyond g_context.min_free_stack_next_task,
                        // just increment err
                        err++;
                    }
                    if ((err == 0) && (iter == g_context.min_free_stack_next_task)) {
                        *name = iter->name;
                        *min_free = iter->min_free_stack_bytes;
                        g_context.min_free_stack_next_task = NULL;
                        err++;
                    }
                }

                // Return the number of tasks _remaining_ in the list,
                // will return ESP_FAIL (-1) if there were no tasks at all
                err--;
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_task_min_free_stack_next()");
        }
    }

    return err;
}

// Stop reading task stack high watermark values.
void fgr_task_min_free_stack_stop()
{
    if (g_context.lock) {
        CONTEXT_LOCK(g_context.lock, "fgr_task_min_free_stack_stop()");
        g_context.min_free_stack_next_task = NULL;
        CONTEXT_UNLOCK(g_context.lock, "fgr_task_min_free_stack_stop()");
    }
}

// End of file

