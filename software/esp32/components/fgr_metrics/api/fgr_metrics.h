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

#ifndef _FGR_METRICS_H_
#define _FGR_METRICS_H_

 /** @file
  * @brief Metrics API for a node of the front garden railway.
  */

#ifdef __cplusplus
extern "C" {
#endif

#include "time.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_METRICS_STACK_MIN_FREE_LOWEST_LENGTH
// The number of stack min free values to report in the
// fgr_metrics_stack_min_free_lowest_t structure.
#  define FGR_METRICS_STACK_MIN_FREE_LOWEST_LENGTH 3
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Structure to carry time.
 */
typedef struct {
    bool since_boot_not_power_cycle;  // If true, seconds is since boot,
                                      // else it is since power cycle.
    time_t seconds;                   // The time in seconds.
} fgr_metrics_time_t;

/** Structure to carry an "event" type metric.
 */
typedef struct {
    uint32_t count;          // Count of occurrences of the event.
    fgr_metrics_time_t time; // The time that the event count was
                             // last incremented.
    int32_t amount;          // An amount associated with the event,
                             // e.g. for a transmit event this might
                             // be the number of bytes transmitted;
                             // may be unused.  This will be ADDED
                             // to the stored amount on each event.
} fgr_metrics_event_t;

/** Structure to track a Boolean event type metric.
 */
typedef struct {
    fgr_metrics_event_t event[2];  //  Index 0 is the "false" event, index 1 is the "true" event
} fgr_metrics_event_bool_t;

/** Structure to carry a stack min free value for a task (in bytes).
 */
typedef struct {
    int32_t min_free_bytes;
    const char *name;  // Task name, will be a null-terminated string
} fgr_metrics_stack_min_free_bytes_t;

/** Structure to carry a set of stack min free values. This
 * should be populated with the FGR_METRICS_STACK_MIN_FREE_LOWEST_LENGTH
 * smallest stack min free values, sorted into ascending order,
 * i.e. the task with the smallest stack min free value should be
 * at index 0.
 */
typedef struct {
    uint8_t count;  // The number of entries populated in task[]
    fgr_metrics_stack_min_free_bytes_t task[FGR_METRICS_STACK_MIN_FREE_LOWEST_LENGTH];
} fgr_metrics_stack_min_free_lowest_t;

/** The metrics: if you add a new item here you should add
 * an entry for it in the union below, with the same name minus
 * the prefix and in lower case, preferably in the same position,
 * and of course a new entry for it in g_metric_type[], g_metric_name[],
 * g_metric_json_name[] and g_metric_time_is_since_boot[].
 *
 * NOTE: do not assign values to any entries as that would stop
 * FGR_METRIC_COUNT working.
 */
typedef enum {
    FGR_METRIC_EVENT_LOCAL_REBOOT,
    FGR_METRIC_EVENT_PANIC,
    FGR_METRIC_EVENT_POWER_BAD,
    FGR_METRIC_EVENT_BOOL_WIFI_CONNECTION,
    FGR_METRIC_EVENT_IP_CONNECTION,
    FGR_METRIC_SIMPLE_WIFI_RSSI_DBM,
    FGR_METRIC_EVENT_BOOL_OTA_CONNECTION,
    FGR_METRIC_EVENT_BOOL_OTA_NVS_WRITE,
    FGR_METRIC_EVENT_BOOL_LOG_SERVER_CONNECTION,
    FGR_METRIC_EVENT_BOOL_CONTROLLER_CONNECTION,
    FGR_METRIC_EVENT_BOOL_CONTROLLER_SOCKET_TX,
    FGR_METRIC_EVENT_CONTROLLER_SOCKET_RX,
    FGR_METRIC_EVENT_BOOL_PING_TX,   // TODO: use this
    FGR_METRIC_EVENT_PING_RX,
    FGR_METRIC_EVENT_BOOL_NVS_WRITE,
    FGR_METRIC_STACK_MIN_FREE_LOWEST,
    FGR_METRIC_SIMPLE_HEAP_MIN_FREE,
    FGR_METRIC_COUNT
} fgr_metrics_t;

/** Union of all possible metrics.
 */
typedef union {
    fgr_metrics_event_t reboot;
    fgr_metrics_event_t local_reboot;
    fgr_metrics_event_t panic;
    fgr_metrics_event_t power_bad;
    fgr_metrics_event_bool_t wifi_connection;         // Event true: connection successful, event false: connection failure, amount unused
    fgr_metrics_event_t ip_connection;
    int32_t wifi_rssi_dbm;
    fgr_metrics_event_bool_t ota_connection;          // Event true: connection successful, event false: connection failure, amount unused
    fgr_metrics_event_bool_t ota_nvs_write;           // Event true: updated version in NVS, event false: failed to update version in NVS, amount unused
    fgr_metrics_event_bool_t log_server_connection;   // Event true: connection successful, event false: connection failure, amount unused
    fgr_metrics_event_bool_t controller_connection;   // Event true: connection successful, event false: connection failure, amount unused
    fgr_metrics_event_bool_t controller_socket_tx;    // Event true: TX successful, event false: TX successful, amount: the number of bytes that [would have been] transmitted
    fgr_metrics_event_t controller_socket_rx;         // Event RX successful, amount: the number of bytes received
    fgr_metrics_event_bool_t ping_tx;                 // Event true: ping TX successful, event false: ping TX failure, amount: the number of bytes that [would have been] transmitted
    fgr_metrics_event_bool_t ping_rx;                 // Event true: ping RX successful, event false: ping RX failure, amount: the number of bytes received
    fgr_metrics_event_bool_t nvs_write;               // Event true: NVS write successful, event false: NVS write failure, amount: the number of bytes that [would have been] written
    fgr_metrics_stack_min_free_lowest_t stack_min_free_lowest;
    int32_t heap_min_free_bytes;
} fgr_metrics_union_t;

/** Generic storage for any metric.
 */
typedef union {
    int32_t simple;
    fgr_metrics_event_t event;
    fgr_metrics_event_bool_t event_bool;
    fgr_metrics_stack_min_free_lowest_t stack_min_free_lowest;
} fgr_metrics_storage_t;

/** Callback for reporting of metrics.
 *
 * @param list   a pointer to a list of metrics, may be NULL.
 *               the list may be indexed using fgr_metrics_t.
 * @param length the number of metrics that metric points to; will
 *               be zero if metric is NULL.
 * @param param  cb_param as passed to fgr_metrics_init().
 */
typedef void (*fgr_metrics_report_cb_t)(fgr_metrics_storage_t *list,
                                        size_t length, void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise metrics.  Needs a task, so fgr_util_init() must
 * have been called first.
 *
 * Note: this will create a semaphore that is never destroyed.
 *
 * @param cb        a callback to be called every
 *                  CONFIG_FGR_METRICS_PERIOD_SECONDS with
 *                  the latest set of updated metrics; you
 *                  might use fgr_metrics_log_cb() here to just
 *                  send all of the metrics as a log message
 *                  to fgr_log, or otherwise you might
 *                  hook in your own function that calls
 *                  fgr_metrics_encode_json() on a selection
 *                  of the metrics, or fgr_metrics_encode_json_all()
 *                  for the lot. Don't spend long in this callback
 *                  as no metric updates will occur until it
 *                  returns.  May be NULL, but that would be
 *                  a bit pointless.
 * @param cb_param  a parameter to pass to the callback; may
 *                  be NULL.
 * @return          ESP_OK on success, else a negative value
 *                  from esp_err_t.
 */
int32_t fgr_metrics_init(fgr_metrics_report_cb_t cb,
                         void *cb_param);

/** A callback that may be hooked-in as the cb parameter to
 * fgr_metrics_init(): NOT TO BE CALLED DIRECTLY.  This callback
 * will log all of the metrics as an ESP_LOGI message.
 *
 * @param list    a pointer to a list of metrics, may be NULL.
 *                the list shall be indexed using fgr_metrics_t.
 * @param length  the number of metrics that metric points to;
 *                SHALL be zero if metric is NULL.
 * @param unused  parameter not unused: only present so that the
 *                function signature matches fgr_metrics_report_cb_t.
 */
void fgr_metrics_log_cb(fgr_metrics_storage_t *list, size_t length,
                        void *unused);

/** Deinitialise metrics.
 */
void fgr_metrics_deinit();

/** Encode a single metric into a null-terminated JSON string.
 *
 * @param metric the metric to encode.
 * @param buffer a place to put the string, cannot be NULL.
 * @param length the amount of storage at buffer, must be greater
 *               than zero.
 * @return       the number of characters required to store the
 *               string (not including the null terminator (i.e.
 *               what strlen() would return)) _irrespective_ of
 *               the value of length.  In other words, if length
 *               is too small the buffer will still be populated
 *               with a null-terminated string but if, on return,
 *               the return value > strlen(buffer), you know you
 *               have a partial string and you can try again with
 *               the correct length.  On failure, a negative value
 *               from esp_err_t will be returned.
 */
int32_t fgr_metrics_encode_json(fgr_metrics_t metric,
                                char *buffer, size_t length);

/** Encode all metrics into a null-terminated JSON string.
 * The string is guaranteed to be NULL terminated.
 *
 * @param buffer a place to put the string, cannot be NULL.
 * @param length the amount of storage at buffer, must be greater
 *               than zero.
 * @return       the number of characters required to store the
 *               string (not including the null terminator (i.e.
 *               what strlen() would return)) _irrespective_ of
 *               the value of length.  In other words, if length
 *               is too small the buffer will still be populated
 *               with a null-terminated string but if, on return,
 *               the return value > strlen(buffer), you know you
 *               have a partial string and you can try again with
 *               the correct length.  On failure, a negative value
 *               from esp_err_t will be returned.
 */
int32_t fgr_metrics_encode_json_all(char *buffer, size_t length);

/** Reset a single metric.
 *
 * @param metric the metric to reset.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_reset(fgr_metrics_t metric);

/** Reset all metrics.
 *
 * @return  ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_reset_all();

/** Set a simple int32_t metric.
 *
 * @param metric the simple metric to set.
 * @param value  the value to set.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_simple_set(fgr_metrics_t metric, int32_t value);

/** Add to a simple int32_t metric.
 *
 * @param metric the simple metric to add to.
 * @param value  the value to add.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_simple_add(fgr_metrics_t metric, int32_t value);

/** Get a simple int32_t metric.
 *
 * @param metric  the simple metric to get.
 * @param value   a pointer to a place to put the simple metric value;
 *                cannot be NULL.
 * @return        ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_simple_get(fgr_metrics_t metric, int32_t *value);

/** Indicate that an event has occurred.
 *
 * @param metric the event metric to set.
 * @param amount an amount associated with the event: use zero
 *               if there is no amount.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_event_set(fgr_metrics_t metric, int32_t amount);

/** Get an event metric.
 *
 * @param metric the event metric to get.
 * @param value  a pointer to a place to put the event metric value;
 *               cannot be NULL.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_event_get(fgr_metrics_t metric,
                              fgr_metrics_event_t *value);

/** Indicate that a Boolean event has occurred.
 *
 * @param metric the Boolean event metric to set.
 * @param value  whether the Boolean event was true or false.
 * @param amount an amount associated with the Boolean event: use zero
 *               if there is no amount.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_event_bool_set(fgr_metrics_t metric, bool value,
                                   int32_t amount);

/** Get a Boolean event metric.
 *
 * @param metric the Boolean event metric to get.
 * @param value  a pointer to a place to put the Boolean metric value;
 *               cannot be NULL.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_event_bool_get(fgr_metrics_t metric,
                                   fgr_metrics_event_bool_t *value);

/** Get the current lowest minimum free stack values.
 *
 * @param value  a place to put the lowest stack values; cannot be NULL.
 * @return       ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_metrics_stack_min_free_lowest_get(fgr_metrics_stack_min_free_lowest_t *value);

/** Get the averaged RSSI value: this is intended to be used as
 * a callback function with fgr_msg_rssi_cb(), but may also be
 * used generally.
 *
 * @param unused not used, only present so that this matches the
 *               function signature fgr_msg_rssi_cb_t.
 * @return       the averaged RSSI value in dBm (0 if not known).
 */
int8_t fgr_metrics_rssi_get(void *unused);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_METRICS_H_

// End of file
