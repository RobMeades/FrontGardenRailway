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
 * @brief Functions to collate and report metrics for a node of the
 * front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "sys/queue.h"
#include "esp_wifi.h" // for esp_wifi_sta_get_rssi()

#include "cJSON.h"

#include "fgr_util.h"
#include "fgr_task.h"
#include "fgr_time.h"
#include "fgr_rram.h"
#include "fgr_network.h"
#include "fgr_metrics.h"

// Required for FGR_LOG_STRING_MAX_LEN
#include "../../../../../protocol/fgr_protocol.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "metrics"

#ifndef FGR_METRICS_TASK_STACK_SIZE
#  define FGR_METRICS_TASK_STACK_SIZE (1024 * 4)
#endif

#ifndef RSSI_BUFFER_LENGTH
// How many readings to average RSSI over.
#  define RSSI_BUFFER_LENGTH 10
#endif

#ifndef FGR_METRICS_STRUCTURE_VERSION
// Metrics structure version: increment this if any of the items
// going into metrics_retained_ram_t change so that the
// data in retained RAM can be reset.
#  define FGR_METRICS_STRUCTURE_VERSION 1
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// The categories of metric; matches the members of fgr_metrics_storage_t.
typedef enum {
    METRIC_TYPE_SIMPLE,
    METRIC_TYPE_EVENT,
    METRIC_TYPE_EVENT_BOOL,
    METRIC_TYPE_STACK_MIN_FREE
} metric_type_t;

// Storage for metrics in retained RAM.
typedef struct {
    int32_t structure_version;
    fgr_metrics_storage_t metrics_list[FGR_METRIC_COUNT];
} storage_t;

// An RSSI buffer.
typedef struct {
    int32_t average_dbm; // The RSSI averaged over the readings
    size_t count;        // How many readings there are
    int8_t readings[RSSI_BUFFER_LENGTH];
} buffer_rssi_t;

// Context.
typedef struct {
    SemaphoreHandle_t lock;
    TaskHandle_t task_handle;
    storage_t storage;
    int64_t last_update_us;
    int64_t last_rssi_measurement_us;
    buffer_rssi_t buffer_rssi;
    fgr_metrics_report_cb_t cb;
    void *cb_param;
} context_t;

// Linked list used by update_stack_min_free_lowest().
typedef struct fgr_metrics_stack_min_free_bytes_entry_t {
    fgr_metrics_stack_min_free_bytes_t task;
    SLIST_ENTRY(fgr_metrics_stack_min_free_bytes_entry_t) next;
} fgr_metrics_stack_min_free_bytes_entry_t;

// Linked list head definition to go with the above.
SLIST_HEAD(fgr_metrics_stack_min_free_bytes_list_t, fgr_metrics_stack_min_free_bytes_entry_t);

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// The names of all the metrics: must have the same number of
// entries as fgr_metrics_t.
static const char *g_metric_name[] = {
    "EVENT_LOCAL_REBOOT",
    "EVENT_PANIC",
    "EVENT_POWER_BAD",
    "EVENT_BOOL_WIFI_CONNECTION",
    "EVENT_IP_CONNECTION",
    "SIMPLE_WIFI_RSSI_DBM",
    "EVENT_BOOL_OTA_CONNECTION",
    "EVENT_BOOL_OTA_NVS_WRITE",
    "EVENT_BOOL_LOG_SERVER_CONNECTION",
    "EVENT_BOOL_CONTROLLER_CONNECTION",
    "EVENT_BOOL_CONTROLLER_SOCKET_TX",
    "EVENT_CONTROLLER_SOCKET_RX",
    "EVENT_BOOL_PING_TX",
    "EVENT_PING_RX",
    "EVENT_BOOL_NVS_WRITE",
    "EVENT_STACK_MIN_FREE_LOWEST",
    "SIMPLE_HEAP_MIN_FREE"
};

// The JSON names of all the metrics: must have the same number of
// entries as fgr_metrics_t.
static const char *g_metric_json_name[] = {
    "lrb",
    "panic",
    "pwr",
    "w",
    "ip",
    "dbm",
    "ota_c",
    "ota_w",
    "log_c",
    "cnt_c",
    "cnt_tx",
    "cnt_rx",
    "ping_tx",
    "ping_rx",
    "nvs_w",
    "stack",
    "heap"
};

// The type of all of the metrics; must have the same number of
// entries as fgr_metrics_t.
static const metric_type_t g_metric_type[] = {
    METRIC_TYPE_EVENT,          // FGR_METRIC_EVENT_LOCAL_REBOOT
    METRIC_TYPE_EVENT,          // FGR_METRIC_EVENT_PANIC
    METRIC_TYPE_EVENT,          // FGR_METRIC_EVENT_POWER_BAD
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_WIFI_CONNECTION
    METRIC_TYPE_EVENT,          // FGR_METRIC_EVENT_IP_CONNECTION
    METRIC_TYPE_SIMPLE,         // FGR_METRIC_SIMPLE_WIFI_RSSI_DBM
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_OTA_CONNECTION
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_OTA_NVS_WRITE
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_LOG_SERVER_CONNECTION
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_CONTROLLER_CONNECTION
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_CONTROLLER_SOCKET_TX
    METRIC_TYPE_EVENT,          // FGR_METRIC_EVENT_CONTROLLER_SOCKET_RX
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_PING_TX
    METRIC_TYPE_EVENT,          // FGR_METRIC_EVENT_PING_RX
    METRIC_TYPE_EVENT_BOOL,     // FGR_METRIC_EVENT_BOOL_NVS_WRITE
    METRIC_TYPE_STACK_MIN_FREE, // FGR_METRIC_STACK_MIN_FREE_LOWEST
    METRIC_TYPE_SIMPLE          // FGR_METRIC_SIMPLE_HEAP_MIN_FREE
};

// A sparsly populated array indicating, for an fgr_metrics_event_t
// (or an fgr_metrics_event_bool_t, which contains an fgr_metrics_event_t),
// whether the metric should be since power cycle (false) or since
// boot (true).
static const bool g_metric_reset_at_boot[] = {
    false,          // FGR_METRIC_EVENT_LOCAL_REBOOT
    false,          // FGR_METRIC_EVENT_PANIC
    false,          // FGR_METRIC_EVENT_POWER_BAD
    true,           // FGR_METRIC_EVENT_BOOL_WIFI_CONNECTION
    true,           // FGR_METRIC_EVENT_BOOL_IP_CONNECTION
    false,          // Don't care (FGR_METRIC_SIMPLE_WIFI_RSSI_DBM)
    false,          // FGR_METRIC_EVENT_BOOL_OTA_CONNECTION
    false,          // FGR_METRIC_EVENT_BOOL_OTA_NVS_WRITE
    true,           // FGR_METRIC_EVENT_BOOL_LOG_SERVER_CONNECTION
    true,           // FGR_METRIC_EVENT_BOOL_CONTROLLER_CONNECTION
    true,           // FGR_METRIC_EVENT_BOOL_CONTROLLER_SOCKET_TX
    true,           // FGR_METRIC_EVENT_CONTROLLER_SOCKET_RX
    true,           // FGR_METRIC_EVENT_BOOL_PING_TX
    true,           // FGR_METRIC_EVENT_PING_RX
    true,           // FGR_METRIC_EVENT_BOOL_NVS_WRITE
    false,          // Don't care (FGR_METRIC_STACK_MIN_FREE_LOWEST)
    false           // Don't care (FGR_METRIC_SIMPLE_HEAP_MIN_FREE)
};

// The reset reasons that are panic reasons.
const esp_reset_reason_t g_reset_reason_panic[] = {ESP_RST_PANIC,
                                                   ESP_RST_INT_WDT,
                                                   ESP_RST_TASK_WDT,
                                                   ESP_RST_WDT
                                                  };

// The reset reasons that are bad power related.
const esp_reset_reason_t g_reset_reason_bad_power[] = {ESP_RST_BROWNOUT,
                                                       ESP_RST_PWR_GLITCH
                                                      };

// Storage for the metrics in retained RAM.
FGR_RRAM_DEFINE(storage_t, storage);

// Context.
static context_t g_context = {0};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Return true if the metric is known and is of the given type.
static bool is_good(fgr_metrics_t metric, metric_type_t type)
{
    return (metric < FGR_UTIL_ARRAY_LENGTH(g_metric_type)) && (type == g_metric_type[metric]);
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: GETTING AND SETTING METRICS
 * -------------------------------------------------------------- */

// Set retained RAM; call this after modifying g_context.storage.
static int32_t retained_ram_set()
{
    // Since the shadow RAM variable is part of our context our
    // shadow variable name does not match that of the retained
    // RAM variable, hence we can't use the FGR_RRAM macros here.
    // Instead, call the function directly using the retained RAM
    // variable name that we know FGR_RRAM_DEFINE() creates.
    return fgr_rram_set(&g_context.storage, sizeof(g_context.storage),
                        &g_storage_rr_container, sizeof(g_storage_rr_container));
}

// Set a simple metric.
static int32_t metric_simple_set(fgr_metrics_storage_t *metrics_list,
                                 fgr_metrics_t metric, int32_t value)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (metrics_list && is_good(metric, METRIC_TYPE_SIMPLE)) {
        (*(metrics_list + metric)).simple = value;
        // This will likely have modified g_context.storage, so set it
        err = retained_ram_set();
    }

    return err;
}

// Get a simple metric.
static int32_t metric_simple_get(fgr_metrics_storage_t *metrics_list,
                                 fgr_metrics_t metric, int32_t *value)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (value && metrics_list && is_good(metric, METRIC_TYPE_SIMPLE)) {
        *value = (*(metrics_list + metric)).simple;
        err = ESP_OK;
    }

    return err;
}

// Set the time value in a metric.
static void metric_set_time(fgr_metrics_storage_t *metrics_list,
                            fgr_metrics_t metric,
                            fgr_metrics_time_t *time)
{
    if (metrics_list && time) {
        time_t time_since_power_on = fgr_time_since_power_on();
        if (time_since_power_on < 0) {
            time_since_power_on = 0;
        }
        time->seconds = time_since_power_on;
        if ((metric < FGR_UTIL_ARRAY_LENGTH(g_metric_reset_at_boot)) &&
                g_metric_reset_at_boot[metric]) {
            time->since_boot_not_power_cycle = true;
            time->seconds = fgr_time_since_boot();
        }
    }
}

// Set an event metric.
static int32_t metric_event_set(fgr_metrics_storage_t *metrics_list,
                                fgr_metrics_t metric, int32_t amount)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (metrics_list && is_good(metric, METRIC_TYPE_EVENT)) {
        fgr_metrics_event_t *event = &(*(metrics_list + metric)).event;
        event->count++;
        metric_set_time(metrics_list, metric, &event->time);
        event->amount = amount;
        // This will likely have modified g_context.storage, so set it
        err = retained_ram_set();
    }

    return err;
}

// Get an event metric.
static int32_t metric_event_get(fgr_metrics_storage_t *metrics_list,
                                fgr_metrics_t metric,
                                fgr_metrics_event_t *value)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (value && metrics_list && is_good(metric, METRIC_TYPE_EVENT)) {
        *value = (*(metrics_list + metric)).event;
        err = ESP_OK;
    }

    return err;
}

// Set a Boolean event metric.
static int32_t metric_event_bool_set(fgr_metrics_storage_t *metrics_list,
                                     fgr_metrics_t metric,
                                     bool value, int32_t amount)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (metrics_list && is_good(metric, METRIC_TYPE_EVENT_BOOL)) {
        fgr_metrics_event_t *event = &((metrics_list + metric)->event_bool.event[0]);
        if (value) {
            event = &((metrics_list + metric)->event_bool.event[1]);
        }
        event->count++;
        metric_set_time(metrics_list, metric, &event->time);
        event->amount = amount;
        // This will likely have modified g_context.storage, so set it
        err = retained_ram_set();
    }

    return err;
}

// Get a Boolean event metric.
static int32_t metric_event_bool_get(fgr_metrics_storage_t *metrics_list,
                                     fgr_metrics_t metric,
                                     fgr_metrics_event_bool_t *value)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (value && metrics_list && is_good(metric, METRIC_TYPE_EVENT_BOOL)) {
        *value = (*(metrics_list + metric)).event_bool;
        err = ESP_OK;
    }

    return err;
}

// Capture the bad reset reasons.
static void reset_reason_set(fgr_metrics_storage_t *metrics_list)
{
    if (metrics_list) {
        esp_reset_reason_t reset_reason = esp_reset_reason();
        for (size_t x = 0; x < FGR_UTIL_ARRAY_LENGTH(g_reset_reason_panic); x++) {
            if (reset_reason == g_reset_reason_panic[x]) {
                metric_event_set(g_context.storage.metrics_list,
                                 FGR_METRIC_EVENT_PANIC, reset_reason);
                break;
            }
        }
        for (size_t x = 0; x < FGR_UTIL_ARRAY_LENGTH(g_reset_reason_bad_power); x++) {
            if (reset_reason == g_reset_reason_bad_power[x]) {
                metric_event_set(g_context.storage.metrics_list,
                                 FGR_METRIC_EVENT_POWER_BAD, reset_reason);
                break;
            }
        }
    }
}

// Reset a metric.
int32_t metric_reset(fgr_metrics_storage_t *metrics_list,
                     fgr_metrics_t metric)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (is_good(metric, METRIC_TYPE_SIMPLE)) {
        err = metric_simple_set(metrics_list, metric, 0);
    } else if (is_good(metric, METRIC_TYPE_EVENT)) {
        memset(&(*(metrics_list + metric)).event, 0, sizeof(fgr_metrics_event_t));
        err = retained_ram_set();
    } else if (is_good(metric, METRIC_TYPE_EVENT_BOOL)) {
        memset(&(*(metrics_list + metric)).event_bool, 0, sizeof(fgr_metrics_event_bool_t));
        err = retained_ram_set();
    } else if (is_good(metric, METRIC_TYPE_STACK_MIN_FREE)) {
        memset(&(*(metrics_list + FGR_METRIC_STACK_MIN_FREE_LOWEST)).stack_min_free_lowest, 0,
               sizeof(fgr_metrics_stack_min_free_lowest_t));
        err = retained_ram_set();
    }

    return err;
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: UPDATING TIMED METRICS
 * -------------------------------------------------------------- */

// Update the average RSSI and record it.
static void update_rssi(buffer_rssi_t *buffer_rssi,
                        fgr_metrics_storage_t *metrics_list,
                        fgr_metrics_t metric)
{
    if (buffer_rssi && metrics_list && fgr_network_is_connected()) {
        // Shift readings (from highest index down)
        for (int32_t x = buffer_rssi->count - 1; x >= 0; x--) {
            buffer_rssi->readings[x + 1] = buffer_rssi->readings[x];
        }

        // Add new reading
        int rssi = 0;
        esp_err_t err = esp_wifi_sta_get_rssi(&rssi);
        if (err == ESP_OK) {
            buffer_rssi->readings[0] = (int8_t) rssi;
            if (buffer_rssi->count < FGR_UTIL_ARRAY_LENGTH(buffer_rssi->readings)) {
                buffer_rssi->count++;
            }
        } else {
            ESP_LOGD(TAG, "Unable to take RSSI reading (%s).", esp_err_to_name(err));
        }

        // Calculate average
        buffer_rssi->average_dbm = 0;
        if (buffer_rssi->count > 0) {
            int32_t sum = 0;
            for (int32_t x = 0; x < buffer_rssi->count; x++) {
                sum += buffer_rssi->readings[x];
            }
            // Denominator has to be signed to produce a signed result
            buffer_rssi->average_dbm = sum / (int32_t) buffer_rssi->count;
        }

        // Record the value
        metric_simple_set(metrics_list, metric, buffer_rssi->average_dbm);
    }
}

// Insert stack_min_free values in ascending order (smallest first),
// called by update_stack_min_free_lowest().
static void update_stack_min_free_insert_sorted(struct fgr_metrics_stack_min_free_bytes_list_t
                                                *list,
                                                fgr_metrics_stack_min_free_bytes_entry_t *new_entry)
{
    if (SLIST_EMPTY(list)) {
        SLIST_INSERT_HEAD(list, new_entry, next);
    } else {
        fgr_metrics_stack_min_free_bytes_entry_t *current = SLIST_FIRST(list);
        fgr_metrics_stack_min_free_bytes_entry_t *prev = NULL;

        // Find position where new_entry should go (smaller values come first)
        while (current != NULL &&
                current->task.min_free_bytes < new_entry->task.min_free_bytes) {
            prev = current;
            current = SLIST_NEXT(current, next);
        }

        if (prev == NULL) {
            // Insert at head (new smallest)
            SLIST_INSERT_HEAD(list, new_entry, next);
        } else {
            // Insert after prev
            SLIST_INSERT_AFTER(prev, new_entry, next);
        }
    }
}

// Update the lowest minimum free stack metric.
static void update_stack_min_free_lowest(fgr_metrics_storage_t *metrics_list)
{
    if (metrics_list) {
        struct fgr_metrics_stack_min_free_bytes_list_t list;
        SLIST_INIT(&list);

        // Get all of the minimum stack extents into a temporary linked list
        fgr_metrics_stack_min_free_bytes_entry_t *entry = (fgr_metrics_stack_min_free_bytes_entry_t *)
                                                          malloc(sizeof(*entry));
        if (entry) {
            int32_t err = fgr_task_min_free_stack_start(&entry->task.name, &entry->task.min_free_bytes);
            if (err >= 0) {
                do {
                    update_stack_min_free_insert_sorted(&list, entry);
                    if (err > 0) {
                        entry = (fgr_metrics_stack_min_free_bytes_entry_t *) malloc(sizeof(*entry));
                        if (!entry) {
                            fgr_task_min_free_stack_stop();
                            break;
                        }
                    }
                    err = fgr_task_min_free_stack_next(&entry->task.name, &entry->task.min_free_bytes);
                } while (err >= 0);
                fgr_task_min_free_stack_stop();
            }
        }

        // Copy the first three entries into the metric
        fgr_metrics_storage_t *storage = metrics_list + FGR_METRIC_STACK_MIN_FREE_LOWEST;
        storage->stack_min_free_lowest.count = 0;
        fgr_metrics_stack_min_free_bytes_entry_t *iter;
        fgr_metrics_stack_min_free_bytes_t *task = storage->stack_min_free_lowest.task;
        SLIST_FOREACH(iter, &list, next) {
            if (storage->stack_min_free_lowest.count < FGR_UTIL_ARRAY_LENGTH(
                    storage->stack_min_free_lowest.task)) {
                task->min_free_bytes = iter->task.min_free_bytes;
                task->name = iter->task.name;
                task++;
                storage->stack_min_free_lowest.count++;
            } else {
                break;
            }
        }
        // This will likely have modified g_context.storage, so set it
        retained_ram_set();

        // Free the temporary list
        while ((entry = SLIST_FIRST(&list)) != NULL) {
            SLIST_REMOVE_HEAD(&list, next);
            free(entry);
        }
    }
}

// Update the minimum free heap metric.
static void update_heap_min_free(fgr_metrics_storage_t *metrics_list)
{
    if (metrics_list) {
        size_t value = heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT);
        metric_simple_set(metrics_list, FGR_METRIC_SIMPLE_HEAP_MIN_FREE, (int32_t) value);
    }
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLBACKS
 * -------------------------------------------------------------- */

// Metrics task.
static void task_metrics_cb(void *handle, void *param)
{
    context_t *context = (context_t *) param;
    storage_t *storage = &context->storage;

    (void) handle;

    CONTEXT_LOCK(context->lock, "task_metrics_cb()");

    int64_t time_us = esp_timer_get_time();

    if (time_us - context->last_rssi_measurement_us > ((CONFIG_FGR_METRICS_RSSI_AVERAGE_TIME_SECONDS *
                                                        1000000) / RSSI_BUFFER_LENGTH)) {
        // Update the RSSI
        update_rssi(&context->buffer_rssi, storage->metrics_list, FGR_METRIC_SIMPLE_WIFI_RSSI_DBM);
        context->last_rssi_measurement_us = time_us;
    }

    if (time_us - context->last_update_us > (CONFIG_FGR_METRICS_PERIOD_SECONDS * 1000000)) {
        // Update the timed metrics

        update_stack_min_free_lowest(storage->metrics_list);
        update_heap_min_free(storage->metrics_list);
        context->last_update_us = time_us;
        if (context->cb) {
            context->cb(storage->metrics_list, FGR_UTIL_ARRAY_LENGTH(g_metric_type), context->cb_param);
        }
    }

    CONTEXT_UNLOCK(context->lock, "task_metrics_cb()");
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: JSON ENCODING
 * -------------------------------------------------------------- */

// JSON encode a simple metric.
static int32_t encode_json_simple(int32_t value, const char *name, cJSON *json)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (name && json) {
        err = -ESP_ERR_NO_MEM;
        if (cJSON_AddNumberToObject(json, name, (double) value)) {
            err = ESP_OK;
        }
    }

    return err;
}

// Just do the inner bit of an event encode: used by
// encode_json_event() and encode_json_event_bool()
static cJSON *encode_json_event_inner(const fgr_metrics_event_t *event)
{
    cJSON *json = NULL;

    if (event) {
        json = cJSON_CreateObject();
        // If there were none of these events, return an empty object
        if (event->count > 0) {
            const char *t = event->time.since_boot_not_power_cycle ? "tb" : "tp";
            if (!json ||
                !cJSON_AddNumberToObject(json, t, (double) event->time.seconds) ||
                !cJSON_AddNumberToObject(json, "n", (double) event->count)) {
                cJSON_Delete(json);
                json = NULL;
            } else {
                // Don't encode amounts that are zero
                if (event->amount > 0) {
                    if (!cJSON_AddNumberToObject(json, "v", (double) event->amount)) {
                        cJSON_Delete(json);
                        json = NULL;
                    }
                }
            }
        }
    }

    return json;
}

// JSON encode an event metric.
static int32_t encode_json_event(const fgr_metrics_event_t *event,
                                 const char *name, cJSON *json)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (event && name && json) {
        err = -ESP_ERR_NO_MEM;
        cJSON *json_inner = encode_json_event_inner(event);
        if (json_inner) {
            if (cJSON_GetArraySize(json_inner) == 0) {
                err = ESP_OK;
                cJSON_Delete(json_inner);
            } else {
                if (cJSON_AddItemToObject(json, name, json_inner)) {
                    err = ESP_OK;
                } else {
                    cJSON_Delete(json_inner);
                }
            }
        }
    }

    return err;
}

// JSON encode a Boolean event metric.
static int32_t encode_json_event_bool(const fgr_metrics_event_bool_t *event_bool,
                                      const char *name, cJSON *json)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    const char *prefix[FGR_UTIL_ARRAY_LENGTH(event_bool->event)] = {"-", "+"};

    if (event_bool && name && json) {
        err = -ESP_ERR_NO_MEM;
        cJSON *json_bool = cJSON_CreateObject();
        if (json_bool) {
            err = ESP_OK;
            for (size_t x = 0; (x < FGR_UTIL_ARRAY_LENGTH(event_bool->event)) && (err == ESP_OK); x++) {
                err = -ESP_ERR_NO_MEM;
                cJSON *json_inner = encode_json_event_inner(&(event_bool->event[x]));
                if (json_inner) {
                    err = ESP_OK;
                    if (cJSON_GetArraySize(json_inner) == 0) {
                        cJSON_Delete(json_inner);
                    } else {
                        if (!cJSON_AddItemToObject(json_bool, prefix[x], json_inner)) {
                            cJSON_Delete(json_inner);
                            err = -ESP_ERR_NO_MEM;
                        }
                    }
                }
            }
            if (cJSON_GetArraySize(json_bool) > 0) {
                if (!cJSON_AddItemToObject(json, name, json_bool)) {
                    cJSON_Delete(json_bool);
                    err = -ESP_ERR_NO_MEM;
                }
            } else {
                cJSON_Delete(json_bool);
            }
        }
    }

    return err;
}

// JSON encode the stack min free metric.
static int32_t encode_json_stack_min_free_lowest(const fgr_metrics_stack_min_free_lowest_t *stack_min_free_lowest,
                                                 const char *name, cJSON *json)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (stack_min_free_lowest && name && json) {
        err = ESP_OK;
        int32_t count = stack_min_free_lowest->count;
        if (count > 0) {
            err = -ESP_ERR_NO_MEM;
            cJSON *json_array = cJSON_CreateArray();
            if (json_array) {
                err = ESP_OK;
                const fgr_metrics_stack_min_free_bytes_t *task = stack_min_free_lowest->task;
                for (size_t x = 0; (x < FGR_UTIL_ARRAY_LENGTH(stack_min_free_lowest->task)) &&
                        (x < count) && (err == ESP_OK); x++) {
                    err = -ESP_ERR_NO_MEM;
                    cJSON *json_task = cJSON_CreateObject();
                    if (json_task) {
                        err = ESP_OK;
                        if (cJSON_AddNumberToObject(json_task, task->name ? task->name : "",
                                                    (double) task->min_free_bytes)) {
                            cJSON_AddItemToArray(json_array, json_task);
                        } else {
                            cJSON_Delete(json_task);
                            err = -ESP_ERR_NO_MEM;
                        }
                    }
                    task++;
                }
                if (err == ESP_OK) {
                    if (!cJSON_AddItemToObject(json, name, json_array)) {
                        cJSON_Delete(json_array);
                        err = -ESP_ERR_NO_MEM;
                    }
                }
            }
        }
    }

    return err;
}

// JSON encode a metric.
static int32_t encode_json(fgr_metrics_storage_t *metrics_list,
                           fgr_metrics_t metric, cJSON *json)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (is_good(metric, METRIC_TYPE_SIMPLE)) {
        err = encode_json_simple((metrics_list + metric)->simple,
                                 g_metric_json_name[metric], json);
    } else if (is_good(metric, METRIC_TYPE_EVENT)) {
        err = encode_json_event(&((metrics_list + metric)->event),
                                g_metric_json_name[metric], json);
    } else if (is_good(metric, METRIC_TYPE_EVENT_BOOL)) {
        err = encode_json_event_bool(&(metrics_list + metric)->event_bool,
                                     g_metric_json_name[metric], json);
    } else if (is_good(metric, METRIC_TYPE_STACK_MIN_FREE)) {
        err = encode_json_stack_min_free_lowest(&(metrics_list + metric)->stack_min_free_lowest,
                                                g_metric_json_name[metric], json);
    }

    return err;
}

// JSON encode all metrics.
static int32_t encode_json_all(fgr_metrics_storage_t *metrics_list,
                               char *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (metrics_list && buffer && (length > 0)) {

        err = -ESP_ERR_NO_MEM;
        cJSON *json = cJSON_CreateObject();
        if (json) {
            err = 0;
            for (size_t x = 0; (x < FGR_METRIC_COUNT) && (err >= 0); x++) {
                err =  encode_json(metrics_list, x, json);
            }
            if (err > 0) {
                err = -ESP_ERR_NO_MEM;
                char *json_str = cJSON_PrintUnformatted(json);
                if (json_str) {
                    // strlcpy() guarantees a terminator and returns the
                    // length of the source string
                    err = strlcpy(buffer, json_str, length);
                    cJSON_free(json_str);
                }
            }
            cJSON_Delete(json);
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise metrics.
int32_t fgr_metrics_init(fgr_metrics_report_cb_t cb,
                         void *cb_param)
{
    int32_t err = ESP_OK;

    // Do some checking
    if (FGR_UTIL_ARRAY_LENGTH(g_metric_type) != FGR_METRIC_COUNT) {
        ESP_LOGE(TAG, "The number of entries in g_metric_type [%d] must match the number of entries in fgr_metrics_t [%d]!",
                 FGR_UTIL_ARRAY_LENGTH(g_metric_type), FGR_METRIC_COUNT);
        err = -ESP_ERR_INVALID_SIZE;
    }
    if (FGR_UTIL_ARRAY_LENGTH(g_metric_name) != FGR_METRIC_COUNT) {
        ESP_LOGE(TAG, "The number of entries in g_metric_name [%d] must match the number of entries in fgr_metrics_t [%d]!",
                 FGR_UTIL_ARRAY_LENGTH(g_metric_name), FGR_METRIC_COUNT);
        err = -ESP_ERR_INVALID_SIZE;
    }
    if (FGR_UTIL_ARRAY_LENGTH(g_metric_json_name) != FGR_METRIC_COUNT) {
        ESP_LOGE(TAG, "The number of entries in g_metric_json_name [%d] must match the number of entries in fgr_metrics_t [%d]!",
                 FGR_UTIL_ARRAY_LENGTH(g_metric_json_name), FGR_METRIC_COUNT);
        err = -ESP_ERR_INVALID_SIZE;
    }
    if (FGR_UTIL_ARRAY_LENGTH(g_metric_reset_at_boot) != FGR_METRIC_COUNT) {
        ESP_LOGE(TAG, "The number of entries in g_metric_reset_at_boot [%d] must match the number of entries in fgr_metrics_t [%d]!",
                 FGR_UTIL_ARRAY_LENGTH(g_metric_reset_at_boot), FGR_METRIC_COUNT);
        err = -ESP_ERR_INVALID_SIZE;
    }

    if (err == ESP_OK) {
        if (!g_context.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_context.lock = xSemaphoreCreateMutex();
        }

        if (g_context.lock) {

            err = ESP_OK;

            CONTEXT_LOCK(g_context.lock, "fgr_metrics_init()");

            if (!g_context.task_handle) {
                // Get the metrics stored in retained RAM into
                // our context
                storage_t storage = {0};
                if ((FGR_RRAM_GET(storage) == ESP_OK) &&
                        (storage.structure_version == FGR_METRICS_STRUCTURE_VERSION)) {
                    // Zero any that should only run from boot
                    for (size_t x = 0; x < FGR_UTIL_ARRAY_LENGTH(g_metric_reset_at_boot); x++) {
                        if (g_metric_reset_at_boot[x]) {
                            metric_reset(storage.metrics_list, x);
                        }
                    }
                    // Copy from the shadow variable into our context
                    g_context.storage = storage;
                } else {
                    // Nothing in retained RAM yet, or the wrong version:
                    // set it up for next time
                    storage.structure_version = FGR_METRICS_STRUCTURE_VERSION;
                    FGR_RRAM_SET(storage);
                }

                reset_reason_set(g_context.storage.metrics_list);

                g_context.cb = cb;
                g_context.cb_param = cb_param;

                // Start the metrics task
                err = fgr_task_create(&task_metrics_cb, &g_context, "metrics",
                                      FGR_METRICS_TASK_STACK_SIZE,
                                      3, &g_context.task_handle);
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_init()");
        }
    }

    return err;
}

// Deinitialise metrics.
void fgr_metrics_deinit()
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_deinit()");
        g_context.cb = NULL;
        g_context.cb_param = NULL;
        // Leave everything else alone so that we can continue
        // to capture metrics during deinitialisation
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_deinit()");
    }
}

// Callback to log all of the metrics as an ESP_LOGI message.
void fgr_metrics_log_cb(fgr_metrics_storage_t *list, size_t length,
                        void *unused)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    char *buffer = NULL;

    (void) unused;

    if (list && (length > 0)) {
        err = -ESP_ERR_NO_MEM;
        buffer = (char *) malloc(FGR_LOG_STRING_MAX_LEN);
        if (buffer) {
            cJSON *json = cJSON_CreateObject();
            if (json) {
                err = 0;
                for (size_t x = 0; x < length && (err >= 0); x++) {
                    err = encode_json(list, x, json);
                }
                if (err >= 0) {
                    err = -ESP_ERR_NO_MEM;
                    char *json_str = cJSON_PrintUnformatted(json);
                    if (json_str) {
                        // strlcpy() guarantees a terminator and returns the
                        // length of the source string
                        strlcpy(buffer, json_str, FGR_LOG_STRING_MAX_LEN);
                        cJSON_free(json_str);
                        ESP_LOGI(TAG, "%s", buffer);
                    }
                }
                cJSON_Delete(json);
            }
            free(buffer);
        }
    }
}

// Encode a single metric into a JSON string.
int32_t fgr_metrics_encode_json(fgr_metrics_t metric,
                                char *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock && buffer && (length > 0)) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_encode_json()");
        err = -ESP_ERR_NO_MEM;
        cJSON *json = cJSON_CreateObject();
        if (json) {
            err =  encode_json(g_context.storage.metrics_list, metric, json);
            if (err == ESP_OK) {
                err = -ESP_ERR_NO_MEM;
                char *json_str = cJSON_PrintUnformatted(json);
                if (json_str) {
                    // strlcpy() guarantees a terminator and returns the
                    // length of the source string
                    err = strlcpy(buffer, json_str, length);
                    cJSON_free(json_str);
                }
            }
            cJSON_Delete(json);
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_encode_json()");
    }

    return err;

}

// Encode all metrics into a JSON string.
int32_t fgr_metrics_encode_json_all(char *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock && buffer && (length > 0)) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_encode_json_all()");
        err = encode_json_all(g_context.storage.metrics_list, buffer, length);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_encode_json_all()");
    }

    return err;
}

// Reset a single metric.
int32_t fgr_metrics_reset(fgr_metrics_t metric)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_reset()");
        err = metric_reset(g_context.storage.metrics_list, metric);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_reset()");
    }

    return err;
}

// Reset all metrics.
int32_t fgr_metrics_reset_all()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_reset_all()");
        memset(g_context.storage.metrics_list, 0, sizeof(g_context.storage.metrics_list));
        err = retained_ram_set();
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_reset_all()");
    }

    return err;
}

// Set a simple metric.
int32_t fgr_metrics_simple_set(fgr_metrics_t metric, int32_t value)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_simple_set()");
        err = metric_simple_set(g_context.storage.metrics_list, metric, value);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_simple_set()");
    }

    return err;
}

// Add to a simple metric.
int32_t fgr_metrics_simple_add(fgr_metrics_t metric, int32_t value)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_simple_add()");
        int32_t current_value = 0;
        err = metric_simple_get(g_context.storage.metrics_list, metric, &current_value);
        if (err == ESP_OK) {
            value += current_value;
            err = metric_simple_set(g_context.storage.metrics_list, metric, value);
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_simple_add()");
    }

    return err;
}

// Get a simple metric.
int32_t fgr_metrics_simple_get(fgr_metrics_t metric, int32_t *value)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_simple_get()");
        err = metric_simple_get(g_context.storage.metrics_list, metric, value);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_simple_get()");
    }

    return err;
}

// Indicate that an event has occurred and set its amount
int32_t fgr_metrics_event_set(fgr_metrics_t metric, int32_t amount)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_event_set()");
        err = metric_event_set(g_context.storage.metrics_list, metric, amount);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_event_set()");
    }

    return err;
}

// Indicate that an event has occurred and add to its amount
int32_t fgr_metrics_event_add(fgr_metrics_t metric, int32_t amount)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_event_add()");
        fgr_metrics_event_t event = {0};
        err = metric_event_get(g_context.storage.metrics_list, metric, &event);
        if (err == ESP_OK) {
            amount += event.amount;
            err = metric_event_set(g_context.storage.metrics_list, metric, amount);
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_event_add()");
    }

    return err;
}

// Get an event metric.
int32_t fgr_metrics_event_get(fgr_metrics_t metric,
                              fgr_metrics_event_t *value)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_event_get()");
        err = metric_event_get(g_context.storage.metrics_list, metric, value);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_event_get()");
    }

    return err;
}

// Indicate that a Boolean event has occurred and set its amount.
int32_t fgr_metrics_event_bool_set(fgr_metrics_t metric, bool value,
                                   int32_t amount)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_event_bool_set()");
        err = metric_event_bool_set(g_context.storage.metrics_list, metric, value, amount);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_event_bool_set()");
    }

    return err;
}

// Indicate that a Boolean event has occurred and add to its amount.
int32_t fgr_metrics_event_bool_add(fgr_metrics_t metric, bool value,
                                   int32_t amount)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_event_bool_add()");
        fgr_metrics_event_bool_t event_bool = {0};
        err = metric_event_bool_get(g_context.storage.metrics_list, metric, &event_bool);
        if (err == ESP_OK) {
            amount += event_bool.event[value].amount;
            err = metric_event_bool_set(g_context.storage.metrics_list, metric,
                                        value, amount);
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_event_bool_add()");
    }

    return err;
}

// Get a Boolean event metric.
int32_t fgr_metrics_event_bool_get(fgr_metrics_t metric,
                                   fgr_metrics_event_bool_t *value)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_event_bool_get()");
        err = metric_event_bool_get(g_context.storage.metrics_list, metric, value);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_event_bool_get()");
    }

    return err;
}

// Get the current lowest minimum free stack values.
int32_t fgr_metrics_stack_min_free_lowest_get(fgr_metrics_stack_min_free_lowest_t *value)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        err = -ESP_ERR_INVALID_ARG;

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_stack_min_free_lowest_get()");
        if (value) {
            *value = (*(g_context.storage.metrics_list +
                        FGR_METRIC_STACK_MIN_FREE_LOWEST)).stack_min_free_lowest;
            err = ESP_OK;
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_stack_min_free_lowest_get()");
    }

    return err;
}

// Get the RSSI value.
int8_t fgr_metrics_rssi_get(void *unused)
{
    int32_t rssi_dbm = 0;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_metrics_rssi_get()");
        metric_simple_get(g_context.storage.metrics_list,
                          FGR_METRIC_SIMPLE_WIFI_RSSI_DBM,
                          &rssi_dbm);
        CONTEXT_UNLOCK(g_context.lock, "fgr_metrics_rssi_get()");
    }

    return (int8_t) rssi_dbm;
}

// End of file
