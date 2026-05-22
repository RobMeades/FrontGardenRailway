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
#include "fgr_monitor.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "monitor"

#ifndef FGR_MONITOR_TASK_STACK_SIZE
#  define FGR_MONITOR_TASK_STACK_SIZE (1024 * 4)
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

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
    struct task_list_t task_list;
} context_task_t;

// Top-level context.
typedef struct {
    SemaphoreHandle_t lock;
    fgr_monitor_cb_t cb;
    void *cb_param;
    TaskHandle_t handle;
    bool running;
    SemaphoreHandle_t running_semaphore;
    context_task_t context_task;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static context_t g_context = {0};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Callback to monitor task state.
static void task_state_cb(fgr_task_state_t state, void *handle,
                          const char *name, void *param)
{
    context_task_t *context_task = (context_task_t *) param;

    if (context_task->lock) {

        CONTEXT_LOCK(context_task->lock, "task_state_cb()");

        task_t *task = NULL;
        bool found = false;
        SLIST_FOREACH(task, &context_task->task_list, next) {
            if (task->handle == handle) {
                found = true;
                break;
            }
        }
        if (!found) {
            // Add a new entry
            task = (task_t *) malloc(sizeof(*task));
            if (task) {
                task->handle = handle;
                task->name = name;
                SLIST_INSERT_HEAD(&context_task->task_list, task, next);
            }
        }
        if (task){
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

        CONTEXT_LOCK(context_task->lock, "task_monitor()");

        // TODO: monitor heap

        // TODO: monitor contact with server

        // Check all of the task watchdog times
        task_t *iter = NULL;
        SLIST_FOREACH(iter, &context_task->task_list, next) {
            if (iter->last_called_us - esp_timer_get_time() > (FGR_MONITOR_WDT_TIMEOUT_SECONDS * 1000000)) {
                // TODO
                break;
            }
        }

        CONTEXT_UNLOCK(context_task->lock, "task_monitor()");

        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(FGR_MONITOR_CHECK_INTERVAL_MS));
    }

    CONTEXT_UNLOCK(context->running_semaphore, "task_monitor() running");

    esp_task_wdt_delete(NULL);
    vTaskDelete(NULL);
}

// Clean up.
static void clean_up()
{
    if (g_context.lock) {

#if FGR_MONITOR_WDT_TIMEOUT_SECONDS > 0
        fgr_task_state_cb(NULL, NULL);
#endif

        CONTEXT_LOCK(g_context.lock, "clean_up() monitor");

         // Flag should stop task running
        g_context.running = false;
        if (g_context.running_semaphore) {
            // Take the running semaphore to know its stopped
            CONTEXT_LOCK(g_context.running_semaphore, "clean_up() monitor task");
            CONTEXT_UNLOCK(g_context.running_semaphore, "clean_up() monitor task");
            vSemaphoreDelete(g_context.running_semaphore);
        }

        context_task_t *context_task = &g_context.context_task;
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

        CONTEXT_UNLOCK(g_context.lock, "clean_up() monitor");
        // The semaphore will be re-used
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise monitoring.
int32_t fgr_monitor_init(fgr_monitor_cb_t cb, void *cb_param)
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

        // Note: we don't use fgr_task_create() here since
        // the monitoring task needs to live on beyond
        // fgr_task_deinit()
        g_context.running_semaphore = xSemaphoreCreateMutex();
        context_task->lock = xSemaphoreCreateMutex();
        if (g_context.running_semaphore && context_task->lock) {
            g_context.running = true;
            if (xTaskCreate(task_monitor, "monitor", FGR_MONITOR_TASK_STACK_SIZE,
                            &g_context, 3, &g_context.handle) == pdPASS) {
                err = ESP_OK;
            } else {
                g_context.running = false;
                g_context.handle = NULL;    // Just in case
                g_context.cb = NULL;
                g_context.cb_param = NULL;
            }
        }

#if FGR_MONITOR_WDT_TIMEOUT_SECONDS > 0
        err = fgr_task_state_cb(task_state_cb, context_task);
#else
        err = ESP_OK;
#endif
        CONTEXT_UNLOCK(g_context.lock, "fgr_monitor_init()");
    }

    if (err != ESP_OK) {
        clean_up();
    }

    return err;
}

// End of file

