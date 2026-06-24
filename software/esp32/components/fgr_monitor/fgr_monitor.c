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
 * @brief Monitor task for a node of the front garden railway.
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

#include "fgr_util.h"
#include "fgr_task.h"
#include "fgr_rram.h"
#include "fgr_metrics.h"
#include "fgr_monitor.h"
#include "fgr_lib.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "monitor"

#ifndef FGR_MONITOR_TASK_STACK_SIZE
#  define FGR_MONITOR_TASK_STACK_SIZE (1024 * 4)
#endif

// The bit to put before the strings of g_abort_reason_name[].
#define ABORT_REASON_NAME_PREFIX "FGR_MONITOR_ABORT_REASON_"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Storage in retained RAM.
typedef struct {
    char task_name[FGR_UTIL_TASK_NAME_MAX_LENGTH];
    fgr_monitor_abort_reason_t abort_reason;
} retained_ram_t;

// Structure that defines all the things we need to monitor a task,
// designed to be used as part of a linked list.
typedef struct task_t {
    TaskHandle_t handle;
    const char *name;
    int64_t last_called_us;
    SLIST_ENTRY(task_t) next;
} task_t;

// Task list head.
SLIST_HEAD(task_list_t, task_t);

// Context for the monitor task.
typedef struct {
    SemaphoreHandle_t lock;
    bool monitor_task_aborting;
    int32_t timeout_seconds;
    int64_t last_msg_receive_us;
    int64_t heap_below_min_start_us;
    struct task_list_t task_list;
} context_task_t;

// Top-level context.
typedef struct {
    SemaphoreHandle_t lock;
    fgr_util_cb_t cb;
    void *cb_param;
    TaskHandle_t handle;
    bool running;
    SemaphoreHandle_t running_semaphore;
    context_task_t context_task;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// The names of the abort reasons: must have the same number of
// entries as fgr_monitor_abort_reason_t.
static const char *g_abort_reason_name[] = {"NONE",
                                            "LOW_MONITOR_TASK_STACK",
                                            "LOW_HEAP",
                                            "FRAGMENTED_HEAP",
                                            "TASK_WDT",
                                            "CONTROLLER_WDT"};

// Storage for abort reason in retained RAM
FGR_RRAM_DEFINE(retained_ram_t, retained_ram);

// Context.
static context_t g_context = {0};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Clean up.
static void clean_up(context_t *context)
{
    if (context->lock) {

#if FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS > 0
        fgr_task_state_cb_set(NULL, NULL);
#endif

        CONTEXT_LOCK(context->lock, "clean_up() monitor");

        // Flag should stop task running
        context->running = false;
        // Can only clean-up the semaphore if we're not being called
        // from within the monitor task itself
        if (context->running_semaphore && (context->handle != xTaskGetCurrentTaskHandle())) {
            // Take the running semaphore to know its stopped
            CONTEXT_LOCK(context->running_semaphore, "clean_up() monitor task");
            CONTEXT_UNLOCK(context->running_semaphore, "clean_up() monitor task");
            vSemaphoreDelete(context->running_semaphore);
        }

        context_task_t *context_task = &context->context_task;
        if (context_task->lock) {

            CONTEXT_LOCK(context_task->lock, "clean_up() monitor task");

            while (!SLIST_EMPTY(&context_task->task_list)) {
                task_t *p = SLIST_FIRST(&context_task->task_list);
                SLIST_REMOVE_HEAD(&context_task->task_list, next);
                free(p);
            }

            CONTEXT_UNLOCK(context_task->lock, "clean_up() monitor task");
            vSemaphoreDelete(context_task->lock);
        }

        context->cb = NULL;
        context->cb_param = NULL;

        CONTEXT_UNLOCK(context->lock, "clean_up() monitor");
        // The semaphore will be re-used
    }
}

// Get the end of an abort reason name: when you print
// it, prefix it with ABORT_REASON_NAME_PREFIX.
static const char *abort_reason_name(int32_t reason)
{
    const char *reason_name = "USER";
    int32_t reason_name_index = -(reason + 1);
    if ((reason_name_index >= 0) &&
        (reason_name_index < FGR_UTIL_ARRAY_LENGTH(g_abort_reason_name))) {
        reason_name = g_abort_reason_name[reason_name_index];
    }

    return reason_name;
}

// Tidy up and call abort.
static void do_abort(int8_t reason, const char *task_name,
                     context_t *context)
{

    char buffer[FGR_UTIL_TASK_NAME_MAX_LENGTH + 10] = {0};
    const char *reason_name = abort_reason_name(reason);

    if (task_name && (strlen(task_name) > 0)) {
        snprintf(buffer, sizeof(buffer), " in task %s", task_name);
    }
    ESP_LOGE(TAG, "%s%s (%d)%s.", ABORT_REASON_NAME_PREFIX,
             reason_name, reason, buffer);

    if (context->lock) {

        CONTEXT_LOCK(context->lock, "do_abort()");
        // Call the user callback, if there is one
        if (context->cb) {
            context->cb(context->cb_param);
        }
        CONTEXT_UNLOCK(context->lock, "do_abort()");
    }

    // The reset reason at boot is set to "normal" for
    // an abort, so we won't pick it up as a panic,
    // instead set a panic event here with our abort
    // reason as the value, negated so as not to overlap
    // with esp_reset_reason_t.
    fgr_metrics_event_set(FGR_METRIC_EVENT_PANIC, -reason);

    fgr_lib_deinit();
    clean_up(context);
    retained_ram_t retained_ram = {0};
    retained_ram.abort_reason = reason;
    if (task_name) {
        strlcpy(retained_ram.task_name, task_name,
                sizeof(retained_ram.task_name));
    } else {
        memset(retained_ram.task_name, 0,
               sizeof(retained_ram.task_name));
    }
    FGR_RRAM_SET(retained_ram);
    abort();
}

// Callback to monitor task state.
static void task_state_cb(fgr_task_state_t state, void *handle,
                          const char *name, void *param)
{
    context_task_t *context_task = (context_task_t *) param;

    if (context_task->lock) {

        CONTEXT_LOCK(context_task->lock, "task_state_cb()");

        task_t *task = NULL;
        task_t *task_prev = NULL;
        bool found = false;
        SLIST_FOREACH(task, &context_task->task_list, next) {
            if (task->handle == handle) {
                found = true;
                break;
            }
            task_prev = task;
        }
        if (!found) {
            if (state != FGR_TASK_STATE_STOPPED) {
                // Add a new entry
                task = (task_t *) malloc(sizeof(*task));
                if (task) {
                    task->handle = handle;
                    task->name = name;
                    SLIST_INSERT_HEAD(&context_task->task_list, task, next);
                }
            }
        } else {
            if (state == FGR_TASK_STATE_STOPPED) {
                // Remove the entry
                if (task_prev == NULL) {
                    // Removing the first element
                    SLIST_REMOVE_HEAD(&context_task->task_list, next);
                } else {
                    // Removing a middle element
                    SLIST_REMOVE_AFTER(task_prev, next);
                }
                task = NULL;
            }
        }
        if (task) {
            task->last_called_us = esp_timer_get_time();
        }

        CONTEXT_UNLOCK(context_task->lock, "task_state_cb()");
    }
}

// Monitor task.
static void task_monitor(void *param)
{
    context_t *context = (context_t *) param;
    context_task_t *context_task = (context_task_t *) &context->context_task;

    esp_task_wdt_add(NULL);

    CONTEXT_LOCK(context->running_semaphore, "task_monitor() running");

    while (context->running) {

        fgr_monitor_abort_reason_t reason = FGR_MONITOR_ABORT_REASON_NONE;
        const char *task_name = NULL;

        CONTEXT_LOCK(context_task->lock, "task_monitor()");

        // First check this task's stack
        if (uxTaskGetStackHighWaterMark(NULL) < FGR_MONITOR_TASK_STACK_SIZE_MIN) {
            reason = FGR_MONITOR_ABORT_REASON_TASK_LOW_STACK;
            task_name = TAG;
        }

        // Monitor heap
        if (reason == FGR_MONITOR_ABORT_REASON_NONE) {
            if (heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) >= FGR_MONITOR_HEAP_BLOCK_MIN) {
                context_task->heap_below_min_start_us = 0;
            } else {
                if (context_task->heap_below_min_start_us == 0) {
                    context_task->heap_below_min_start_us = esp_timer_get_time();
                } else if (context_task->heap_below_min_start_us - esp_timer_get_time() >
                           FGR_MONITOR_HEAP_MIN_DURATION_SECONDS * 1000000) {
                    reason = FGR_MONITOR_ABORT_REASON_FRAGMENTED_HEAP;
                }
            }
        }
        if ((reason == FGR_MONITOR_ABORT_REASON_NONE) &&
            (heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT) < FGR_MONITOR_HEAP_MIN)) {
            reason = FGR_MONITOR_ABORT_REASON_LOW_HEAP;
        }

        // Monitor communications with the controller
        if ((reason == FGR_MONITOR_ABORT_REASON_NONE) &&
            (context_task->last_msg_receive_us - esp_timer_get_time() >
            FGR_MONITOR_WDT_CONTROLLER_TIMEOUT_SECONDS * 1000000)) {
            reason = FGR_MONITOR_ABORT_REASON_CONTROLLER_WDT;
        }

        if (reason == FGR_MONITOR_ABORT_REASON_NONE) {
            // Check all of the task last_called_us times
            task_t *iter = NULL;
            SLIST_FOREACH(iter, &context_task->task_list, next) {
                if (iter->last_called_us - esp_timer_get_time() > (((int64_t) context_task->timeout_seconds) * 1000000)) {
                    task_name = iter->name;
                    reason = FGR_MONITOR_ABORT_REASON_TASK_WDT;
                    break;
                }
            }
        }

        if (reason != FGR_MONITOR_ABORT_REASON_NONE) {
            // Set this flag while the context is locked
            // so that any task waiting for the mutex
            // lock that is inside fgr_monitor_task_wdt_feed()
            // gets to see it once they have their lock
            context_task->monitor_task_aborting = true;
        }

        CONTEXT_UNLOCK(context_task->lock, "task_monitor()");

        // Do this after unlocking the mutex as do_abort()
        // will call fgr_lib_deinit(), which destroys tasks,
        // tasks that may have been waiting on
        // fgr_monitor_task_wdt_feed() to complete
        if (reason != FGR_MONITOR_ABORT_REASON_NONE) {
            do_abort(reason, task_name, context);
        }

        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(FGR_MONITOR_CHECK_INTERVAL_MS));
    }

    CONTEXT_UNLOCK(context->running_semaphore, "task_monitor() running");

    esp_task_wdt_delete(NULL);
    vTaskDelete(NULL);
}

// Set the ESP-IDF watchdog timeout.
static void espidf_wdt_set(int32_t timeout_seconds)
{
#if defined (CONFIG_ESP_TASK_WDT_TIMEOUT_S)
    esp_task_wdt_config_t wdt_cfg = {
        .timeout_ms = timeout_seconds * 1000,
#  if defined CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0
        .idle_core_mask = 1 << CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0,
#  else
        .idle_core_mask = 0,
#  endif
#  if defined CONFIG_ESP_TASK_WDT_PANIC
        .trigger_panic = true
#  else
        .trigger_panic = false
#  endif
    };

    esp_task_wdt_reconfigure(&wdt_cfg);
#endif
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise monitoring.
int32_t fgr_monitor_init(fgr_util_cb_t cb, void *cb_param)
{
    int32_t err = ESP_OK;
    context_task_t *context_task = &g_context.context_task;

    if (!g_context.lock) {
        // Create mutex
        err = -ESP_ERR_NO_MEM;
        g_context.lock = xSemaphoreCreateMutex();
        SLIST_INIT(&context_task->task_list);
    }

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_monitor_init()");

        g_context.cb = cb;
        g_context.cb_param = cb_param;
        context_task->monitor_task_aborting = false;
        context_task->timeout_seconds = FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS;

        // Set up retained storage if required
        retained_ram_t retained_ram;
        if (FGR_RRAM_GET(retained_ram) != ESP_OK) {
            retained_ram.abort_reason = FGR_MONITOR_ABORT_REASON_NONE;
            FGR_RRAM_SET(retained_ram);
        }

        // Note: we don't use fgr_task_create() here since
        // the monitoring task needs to live on beyond
        // fgr_task_deinit()
        g_context.running_semaphore = xSemaphoreCreateMutex();
        context_task->lock = xSemaphoreCreateMutex();
        if (g_context.running_semaphore && context_task->lock) {
            g_context.running = true;
            if (xTaskCreate(task_monitor, TAG, FGR_MONITOR_TASK_STACK_SIZE,
                            &g_context, 3, &g_context.handle) == pdPASS) {
                err = ESP_OK;
            } else {
                g_context.running = false;
                g_context.handle = NULL;    // Just in case
            }
        }

#if FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS > 0
        err = fgr_task_state_cb_set(task_state_cb, context_task);
#else
        err = ESP_OK;
#endif
        CONTEXT_UNLOCK(g_context.lock, "fgr_monitor_init()");
    }

    if (err != ESP_OK) {
        clean_up(&g_context);
    }

    return err;
}

// Feed the monitor task watchdog.
void fgr_monitor_task_wdt_feed(void *handle)
{
    context_task_t *context_task = &g_context.context_task;

    if (esp_task_wdt_status(NULL) == ESP_OK) {
        // Feed the HW watchdog
        esp_task_wdt_reset();
    }

    if (handle && context_task->lock) {

        CONTEXT_LOCK(context_task->lock, "fgr_monitor_task_wdt_feed()");

        if (!context_task->monitor_task_aborting) {
            task_t *task = NULL;
            SLIST_FOREACH(task, &context_task->task_list, next) {
                if (task->handle == handle) {
                    task->last_called_us = esp_timer_get_time();
                    break;
                }
            }
        }

        CONTEXT_UNLOCK(context_task->lock, "fgr_monitor_task_wdt_feed()");
    }
}

// Get the monitor task watchdog timeout.
int32_t fgr_monitor_task_wdt_timeout_get()
{
    int32_t timeout_seconds = -ESP_ERR_INVALID_STATE;
    context_task_t *context_task = &g_context.context_task;

    if (context_task->lock) {

        CONTEXT_LOCK(context_task->lock, "fgr_monitor_task_wdt_timeout_get()");
        timeout_seconds = context_task->timeout_seconds;
        CONTEXT_UNLOCK(context_task->lock, "fgr_monitor_task_wdt_timeout_get()");
    }

    return timeout_seconds;
}

// Set the current monitor task watchdog timeout.
int32_t fgr_monitor_task_wdt_timeout_set(int32_t timeout_seconds)
{
    int32_t timeout_seconds_returned = -ESP_ERR_INVALID_STATE;
    context_task_t *context_task = &g_context.context_task;

    if (context_task->lock) {

        CONTEXT_LOCK(context_task->lock, "fgr_monitor_task_wdt_timeout_set()");
        if (timeout_seconds > FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_MAX) {
            timeout_seconds = FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_MAX;
        }
        context_task->timeout_seconds = timeout_seconds;
        espidf_wdt_set(context_task->timeout_seconds + FGR_MONITOR_WDT_TASK_TIMEOUT_SECONDS_ADVANCE);
        timeout_seconds_returned = context_task->timeout_seconds;
        CONTEXT_UNLOCK(context_task->lock, "fgr_monitor_task_wdt_timeout_set()");
    }

    return timeout_seconds_returned;
}

// Message receive callback.
void fgr_monitor_msg_receive_cb(void *unused)
{
    context_task_t *context_task = &g_context.context_task;

    (void) unused;

    if (context_task->lock) {

        CONTEXT_LOCK(context_task->lock, "fgr_monitor_msg_receive_cb()");
        context_task->last_msg_receive_us = esp_timer_get_time();
        CONTEXT_UNLOCK(context_task->lock, "fgr_monitor_msg_receive_cb()");
    }
}

// Cause an abort.
void fgr_monitor_abort(uint8_t reason, const char *task_name)
{
    do_abort((int8_t) (reason & 0x7f), task_name, &g_context);
}

// Obtain the reason for a monitor abort.
int32_t fgr_monitor_abort_reason_get(char *task_name)
{
    int32_t reason = FGR_MONITOR_ABORT_REASON_NONE;
    retained_ram_t retained_ram = {0};

    if (FGR_RRAM_GET(retained_ram) == ESP_OK) {
        reason = retained_ram.abort_reason;
        if (task_name) {
            strlcpy(task_name, retained_ram.task_name,
                    FGR_UTIL_TASK_NAME_MAX_LENGTH);
        }
        retained_ram.abort_reason = FGR_MONITOR_ABORT_REASON_NONE;
        FGR_RRAM_SET(retained_ram);
    }

    return reason;
}

// Function to log a monitor abort.
int32_t fgr_monitor_abort_reason_log(const char *tag, const char *prefix,
                                     esp_log_level_t level)
{
    int32_t err = ESP_OK;
    char task_name[FGR_UTIL_TASK_NAME_MAX_LENGTH] = {0};
    char buffer[FGR_UTIL_TASK_NAME_MAX_LENGTH + 10] = {0};

    int32_t reason = fgr_monitor_abort_reason_get(task_name);
    if (reason != FGR_MONITOR_ABORT_REASON_NONE) {
        if (level > ESP_LOG_NONE) {
            if (!tag) {
                tag = TAG;
            }
            if (!prefix) {
                prefix = "";
            }
            if (strlen(task_name) > 0) {
                snprintf(buffer, sizeof(buffer), " in task %s", task_name);
            }
            const char *reason_name_prefix = ABORT_REASON_NAME_PREFIX;
            const char *reason_name = abort_reason_name(reason);
            switch (level) {
                case ESP_LOG_ERROR:
                    ESP_LOGE(tag, "%s%s%s (%d)%s", prefix, reason_name_prefix,
                             reason_name, reason, buffer);
                    break;
                case ESP_LOG_WARN:
                    ESP_LOGW(tag, "%s%s%s (%d)%s", prefix, reason_name_prefix,
                             reason_name, reason, buffer);
                    break;
                case ESP_LOG_INFO:
                    ESP_LOGI(tag, "%s%s%s (%d)%s", prefix, reason_name_prefix,
                             reason_name, reason, buffer);
                    break;
                case ESP_LOG_DEBUG:
                    ESP_LOGD(tag, "%s%s%s (%d)%s", prefix, reason_name_prefix,
                             reason_name, reason, buffer);
                    break;
                case ESP_LOG_VERBOSE:
                    ESP_LOGD(tag, "%s%S%s (%d)%s", prefix, reason_name_prefix,
                             reason_name, (int) reason, buffer);
                default:
                    break;
            }
        }
        err = 1;
    }

    return err;
}

// End of file
