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
 * @brief Implementation of logging that includes forwarding
 * to the controller for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_log.h"
#include "arpa/inet.h"
#include "errno.h"

#include "fgr_util.h"
#include "fgr_socket.h"
#include "fgr_msg.h"
#include "fgr_nvs.h"
#include "fgr_log.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "log"

#ifndef LOG_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS
// Idle time before TCP keep-alive kicks in on the logging socket,
// in seconds.
#  define LOG_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS   10
#endif

#ifndef LOG_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS
// Keep alive probe interval on the logging socket, in seconds.
#  define LOG_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS   5
#endif

#ifndef LOG_SOCKET_TCP_KEEP_ALIVE_COUNT
// The number of TCP probes to lose before considering the
// logging socket connection dead.
#  define LOG_SOCKET_TCP_KEEP_ALIVE_COUNT   3
#endif

#ifndef FGR_MSG_TASK_LOG_STACK_SIZE
#  define FGR_MSG_TASK_LOG_STACK_SIZE (1024 * 4)
#endif

#ifndef LOG_QUEUE_LENGTH
// How many messages to hold in the queue, each of size queue_msg_t.
#  define LOG_QUEUE_LENGTH 100
#endif

#ifndef NVS_NAME_LOG_ON_NOT_OFF
// A name for the logging on/off field in NV storage.
#  define NVS_NAME_LOG_ON_NOT_OFF "log_on_not_off"
#endif

#ifndef NVS_NAME_LOG_LEVEL_MIN
// A name for the minimum logging level field in NV storage.
#  define NVS_NAME_LOG_LEVEL_MIN "log_level_min"
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// A message to be forwarded on the logging queue.
typedef struct {
    fgr_msg_header_log_t header;
    size_t body_length; // Need this since the length inside body is htonl() encoded, includes the length field itself
    fgr_msg_body_t *body;
} queue_msg_t;


// Buffer for logs when disconnected.
typedef struct {
    fgr_msg_header_log_t **headers;  // Array of header pointers
    fgr_msg_body_t **bodies;         // Array of body pointers
    uint16_t size;                   // Max entries (FGR_LOG_BUFFER_MAX_ENTRIES)
    uint16_t head;                   // Write position
    uint16_t tail;                   // Read position (oldest)
    bool full;
    uint32_t dropped_count;          // Messages dropped due to full buffer
} buffer_t;

// Context.
typedef struct {
    fgr_log_level_t level_min;  // Minimum level to forward
    bool on_not_off;
    int sock;
    void *context_sock;
    bool connected;
    SemaphoreHandle_t lock;
    TaskHandle_t task;
    QueueHandle_t queue;
    bool running;
    buffer_t buffer;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static context_t g_context = {
    .sock = -1,
    .level_min = FGR_LOG_LEVEL_INFO,  // Default: forward INFO and above
    .on_not_off = true
};

// Table to convert an fgr_log_level_t (the index) into an esp_log_level_t.
static const esp_log_level_t g_fgr_to_esp_log_level[] = {ESP_LOG_DEBUG,  // FGR_LOG_LEVEL_DEBUG (0)
                                                         ESP_LOG_INFO,   // FGR_LOG_LEVEL_INFO (1)
                                                         ESP_LOG_WARN,   // FGR_LOG_LEVEL_WARN (2)
                                                         ESP_LOG_ERROR}; // FGR_LOG_LEVEL_ERROR (3)

// Table to convert an esp_log_level_t (the index) into an fgr_log_level_t.
static const fgr_log_level_t g_esp_to_fgr_log_level[] = {FGR_LOG_LEVEL_ERROR,  // ESP_LOG_NONE (0)
                                                         FGR_LOG_LEVEL_ERROR,  // ESP_LOG_ERROR (1)
                                                         FGR_LOG_LEVEL_WARN,   // ESP_LOG_WARN (2)
                                                         FGR_LOG_LEVEL_INFO,   // ESP_LOG_INFO (3)
                                                         FGR_LOG_LEVEL_DEBUG,  // ESP_LOG_DEBUG (4)
                                                         FGR_LOG_LEVEL_DEBUG}; // ESP_LOG_VERBOSE (5)

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Convert an fgr_log_level_t into an esp_log_level_t.
static esp_log_level_t fgr_to_esp_log_level(fgr_log_level_t fgr_log_level)
{
    esp_log_level_t esp_log_level = ESP_LOG_ERROR;

    if (fgr_log_level < FGR_UTIL_ARRAY_LENGTH(g_fgr_to_esp_log_level)) {
        esp_log_level = g_fgr_to_esp_log_level[fgr_log_level];
    }

    return esp_log_level;
}

// Convert an esp_log_level_t into an fgr_log_level_t.
static fgr_log_level_t esp_to_fgr_log_level(esp_log_level_t esp_log_level)
{
    fgr_log_level_t fgr_log_level = FGR_LOG_LEVEL_DEBUG;

    if (esp_log_level < FGR_UTIL_ARRAY_LENGTH(g_esp_to_fgr_log_level)) {
        fgr_log_level = g_esp_to_fgr_log_level[esp_log_level];
    }

    return fgr_log_level;
}

// Wot it says.
static void clean_up()
{
    // Restore default logging
    esp_log_set_vprintf(vprintf);

    if (g_context.lock) {

        ESP_LOGI(TAG, "Stopping log forwarding.");

        g_context.running = false;

        CONTEXT_LOCK(g_context.lock, "clean_up() log");

        g_context.connected = false;

        if (g_context.task) {
            // Wait for the log task to exit
            vTaskDelay(pdMS_TO_TICKS(1000));
            g_context.task = NULL;
        }

        if (g_context.queue) {
            queue_msg_t msg;
            while (xQueueReceive(g_context.queue, &msg, 0) == pdTRUE) {
                free(msg.body);
                vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
                esp_task_wdt_reset();
            }
            vQueueDelete(g_context.queue);
            g_context.queue = NULL;
        }

        // Clean up the disconnect buffer
        if (g_context.buffer.headers) {
            // Free any remaining buffered messages
            uint16_t idx = g_context.buffer.tail;
            uint16_t count = g_context.buffer.full ? g_context.buffer.size :
                             (g_context.buffer.head >= g_context.buffer.tail ?
                              g_context.buffer.head - g_context.buffer.tail :
                              g_context.buffer.size - g_context.buffer.tail + g_context.buffer.head);
            for (size_t x = 0; (x < count) && (x < g_context.buffer.size); x++) {
                free(g_context.buffer.headers[idx]);
                free(g_context.buffer.bodies[idx]);
                idx = (idx + 1) % g_context.buffer.size;
            }
            free(g_context.buffer.headers);
            free(g_context.buffer.bodies);
            g_context.buffer.headers = NULL;
            g_context.buffer.bodies = NULL;
        }

        // Lose the socket
        fgr_socket_channel_stop(&g_context.context_sock);
        g_context.sock = -1;

        CONTEXT_UNLOCK(g_context.lock, "clean_up() log");
        // Don't delete the semaphore, someone might have it still
    }
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: NVS RELATED
 * -------------------------------------------------------------- */

// Retrieve whether logging is on or off from NVS.
static int32_t nvs_on_not_off_get(bool *log_on_not_off)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint32_t value = 0;

    if (log_on_not_off) {
        err = fgr_nvs_get(NVS_NAME_LOG_ON_NOT_OFF, &value);
        if (err == ESP_OK) {
            *log_on_not_off = (value != 0);
        }
    }

    return err;
}

// Set whether logging is on or off in NVS.
static int32_t nvs_on_not_off_set(bool log_on_not_off)
{
    return fgr_nvs_set(NVS_NAME_LOG_ON_NOT_OFF, log_on_not_off);
}

// Retrieve the minimum logging level from NVS.
static int32_t nvs_level_min_get(fgr_log_level_t *log_level_min)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint32_t value = 0;

    if (log_level_min) {
        err = fgr_nvs_get(NVS_NAME_LOG_LEVEL_MIN, &value);
        if (err == ESP_OK) {
            *log_level_min = (fgr_log_level_t) value;
        }
    }

    return err;
}

// Set the minimum logging level in NVS.
static int32_t nvs_level_min_set(fgr_log_level_t log_level_min)
{
    return fgr_nvs_set(NVS_NAME_LOG_LEVEL_MIN, log_level_min);
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: BUFFERED LOGGING (FOR WHEN DISCONNECTED)
 * -------------------------------------------------------------- */

// Initialise the buffer used for logging when disconnected.
static int32_t log_buffer_init(buffer_t *buffer)
{
    int32_t err = ESP_OK;

    buffer->size = FGR_LOG_BUFFER_MAX_ENTRIES;
    buffer->headers = calloc(buffer->size, sizeof(fgr_msg_header_log_t *));
    buffer->bodies = calloc(buffer->size, sizeof(fgr_msg_body_t *));

    if (!buffer->headers || !buffer->bodies) {
        free(buffer->headers);
        free(buffer->bodies);
        buffer->headers = NULL;
        buffer->bodies = NULL;
        err = -ESP_ERR_NO_MEM;
    } else {
        buffer->head = 0;
        buffer->tail = 0;
        buffer->full = false;
        buffer->dropped_count = 0;

        ESP_LOGI(TAG, "Disconnect buffer initialized with %d entries",
                 buffer->size);
    }

    return err;
}

// Add a message to the disconnect buffer (called from task_log).
// IMPORTANT: context should be locked before this is called.
static void log_buffer_add(buffer_t *buffer,
                           fgr_msg_header_log_t *header,
                           fgr_msg_body_t *body,
                           size_t body_length)
{
    fgr_msg_header_log_t *header_copy = NULL;
    fgr_msg_body_t *body_copy = NULL;

    if (buffer->headers) {
        header_copy = malloc(sizeof(fgr_msg_header_log_t));
        if (header_copy) {
            body_copy = malloc(body_length);
            if (body_copy) {
                memcpy(header_copy, header, sizeof(fgr_msg_header_log_t));
                memcpy(body_copy, body, body_length);

                if (buffer->full) {
                    free(buffer->headers[buffer->tail]);
                    free(buffer->bodies[buffer->tail]);
                    buffer->tail = (buffer->tail + 1) % buffer->size;
                    buffer->dropped_count++;
                }

                buffer->headers[buffer->head] = header_copy;
                buffer->bodies[buffer->head] = body_copy;
                buffer->head = (buffer->head + 1) % buffer->size;

                if (buffer->head == buffer->tail) {
                    buffer->full = true;
                }

                // Success - prevent cleanup
                header_copy = NULL;
                body_copy = NULL;
            }
        }

        if (header_copy) {
            free(header_copy);
        }
        if (body_copy) {
            free(body_copy);
        }
        if (!header_copy || !body_copy) {
            buffer->dropped_count++;
        }
    }
}

// Replay buffered messages from when we were disconnected.
// IMPORTANT: context SHOULD NOT be locked before this is called.
static void log_buffer_replay(context_t *context)
{
    fgr_msg_t replay_msg;
    uint16_t count = 0;
    uint16_t sent = 0;
    uint16_t idx = 0;
    uint16_t start_idx = 0;
    uint16_t cleanup_idx = 0;
    buffer_t *buffer = &context->buffer;

    if (buffer->headers) {
        // All manual semaphore calls in this function,
        // since we need to give the semaphore as much
        // as we can
        xSemaphoreTake(context->lock, portMAX_DELAY);

        count = buffer->full ? buffer->size :
                (buffer->head >= buffer->tail ?
                 buffer->head - buffer->tail :
                 buffer->size - buffer->tail + buffer->head);

        if (count > 0) {
            ESP_LOGI(TAG, "Replaying %d buffered log message(s) (dropped %lu total)",
                     count, buffer->dropped_count);

            start_idx = buffer->tail;
            xSemaphoreGive(context->lock);

            idx = start_idx;
            sent = 0;

            while (sent < count) {
                xSemaphoreTake(context->lock, portMAX_DELAY);

                if (idx >= buffer->size) {
                    idx = 0;
                }

                replay_msg.header.log = *buffer->headers[idx];
                uint32_t body_length_net = buffer->bodies[idx]->length;
                size_t body_length = ntohl(body_length_net) + sizeof(body_length_net);
                memcpy(&replay_msg.body, buffer->bodies[idx], body_length);

                xSemaphoreGive(context->lock);

                fgr_socket_send(context->sock, &replay_msg,
                                sizeof(replay_msg.header) + body_length, 0);

                idx++;
                sent++;

                if ((sent & 31) == 0) {
                    esp_task_wdt_reset();
                    vTaskDelay(1);
                }
            }

            xSemaphoreTake(context->lock, portMAX_DELAY);

            cleanup_idx = buffer->tail;
            for (uint16_t i = 0; i < count; i++) {
                free(buffer->headers[cleanup_idx]);
                free(buffer->bodies[cleanup_idx]);
                cleanup_idx = (cleanup_idx + 1) % buffer->size;
            }

            buffer->head = 0;
            buffer->tail = 0;
            buffer->full = false;
            buffer->dropped_count = 0;

            xSemaphoreGive(context->lock);

            ESP_LOGI(TAG, "Replay complete, sent %d message(s).", sent);
        } else {
            xSemaphoreGive(context->lock);
        }
    }
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: TASK
 * -------------------------------------------------------------- */

// Log task.
static void task_log(void *param)
{
    context_t *context = (context_t *) param;
    fgr_msg_t log_msg;
    queue_msg_t queue_msg;

    esp_task_wdt_add(NULL);

    while (context->running) {

        CONTEXT_LOCK(context->lock, "task_log()");

        // Process all pending messages (non-blocking)
        while (xQueueReceive(context->queue, &queue_msg, 0) == pdTRUE) {

            // Check if this is an FGR_MSG_TYPE_NULL message type (internal replay signal)
            uint16_t msg_type = ntohs(queue_msg.header.type);

            if (msg_type == (FGR_MSG_TYPE_NULL << 12)) {
                // This is a replay request - release lock and handle it
                xSemaphoreGive(context->lock);
                log_buffer_replay(context);
                xSemaphoreTake(context->lock, portMAX_DELAY);
            } else {
                if (context->connected) {
                    log_msg.header.log = queue_msg.header;
                    memcpy(&log_msg.body, queue_msg.body, queue_msg.body_length);
                    if (fgr_socket_send(context->sock, &log_msg,
                                        sizeof(log_msg.header) + queue_msg.body_length, 0) == ESP_OK) {
                        fgr_socket_channel_activity(&context->context_sock);
                    } else {
                        fgr_socket_channel_failed(&context->context_sock);
                        log_buffer_add(&context->buffer, &queue_msg.header,
                                       queue_msg.body, queue_msg.body_length);
                        context->connected = false;
                    }
                } else {
                    // Disconnected: buffer instead of dropping
                    log_buffer_add(&context->buffer, &queue_msg.header,
                                   queue_msg.body, queue_msg.body_length);
                }

                free(queue_msg.body);
            }
        }

        CONTEXT_UNLOCK(context->lock, "task_log()");

        vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
        esp_task_wdt_reset();
    }

    ESP_LOGI(TAG, "Log task exiting.");
    esp_task_wdt_delete(NULL);
    vTaskDelete(NULL);
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLBACKS
 * -------------------------------------------------------------- */

// Custom vprintf handler with level filtering.
static int tcp_log_vprintf(const char *fmt, va_list args)
{
    queue_msg_t queue_msg = {0};
    fgr_msg_header_log_t *header = &queue_msg.header;
    fgr_msg_body_t **body = &(queue_msg.body);

    if (g_context.running && g_context.on_not_off) {
        // Parse the log level from format string
        // ESP-IDF logs start with level character: "I (123) TAG: message"
        esp_log_level_t esp_log_level = ESP_LOG_INFO;
        if (fmt[0] == 'E') {
            esp_log_level = ESP_LOG_ERROR;
        } else if (fmt[0] == 'W') {
            esp_log_level = ESP_LOG_WARN;
        } else if (fmt[0] == 'I') {
            esp_log_level = ESP_LOG_INFO;
        } else if (fmt[0] == 'D') {
            esp_log_level = ESP_LOG_DEBUG;
        } else if (fmt[0] == 'V') {
            esp_log_level = ESP_LOG_VERBOSE;
        }

        // Convert to protocol log level
        fgr_log_level_t fgr_log_level = esp_to_fgr_log_level(esp_log_level);

        if (fgr_log_level >= g_context.level_min) {
            *body = (fgr_msg_body_t *) malloc(sizeof(**body));
            if (*body) {
                // Format the message
                int32_t length = vsnprintf((char *) ((*body)->contents), FGR_LOG_STRING_MAX_LEN, fmt, args);

                if (length > FGR_LOG_STRING_MAX_LEN) {
                    length = FGR_LOG_STRING_MAX_LEN;
                }

                // Strip off any trailing linefeed
                if ((length > 0) && ((*body)->contents[length - 1] == '\n')) {
                    length--;
                }

                // Assemble and forward
                header->type = htons(((uint16_t) FGR_MSG_TYPE_LOG) << 12);
                header->level = fgr_log_level;
                (*body)->length = htonl((uint32_t) length);
                queue_msg.body_length = length + sizeof((*body)->length);
                (*body)->contents[length] = 0; // Ensure terminator

                // Should really lock the context here but if we do that
                // and xQueueSend() blocks we're stuffed 'cos the log task
                // won't be able to get the lock to send it.
                // Just have to make sure we don't pull the rug out
                // from under ourselves in clean_up()...?
                if (xQueueSend(g_context.queue, &queue_msg, portMAX_DELAY) != pdPASS) {
                    free(*body);
                }
            }
        }
    }

    // Always output to UART for local debugging
    return vprintf(fmt, args);
}

// Callback to send a heartbeat log, called by
// fgr_socket_channel_maintain().
static void socket_heartbeat_cb(int sock, void *param)
{
    context_t *context = (context_t *) param;
    (void) sock;

    if (context->lock) {
        ESP_LOGI(TAG, "Log heartbeat.");
        CONTEXT_LOCK(context->lock, "socket_heartbeat_cb() log");
        fgr_socket_channel_activity(&context->context_sock);
        CONTEXT_UNLOCK(context->lock, "socket_heartbeat_cb() log");
    }
}

// Callback on socket reconnection, called by
// fgr_socket_channel_maintain().
static void socket_reconnect_cb(int sock, void *param)
{
    context_t *context = (context_t *) param;

    if (context->lock) {

        int32_t err = fgr_socket_enable_tcp_keep_alive(sock,
                                                       LOG_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS,
                                                       LOG_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS,
                                                       LOG_SOCKET_TCP_KEEP_ALIVE_COUNT);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "fgr_socket_enable_tcp_keep_alive() returned error: %s.", esp_err_to_name(err));
        }
        err = fgr_socket_enable_tcp_no_delay(sock);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "fgr_socket_enable_tcp_no_delay() returned error: %s.", esp_err_to_name(err));
        }

        CONTEXT_LOCK(context->lock, "socket_reconnect_cb() log");

        context->sock = sock;
        context->connected = true;

        if (context->queue) {
            // If we have a queue, we are configured and running,
            // so queue an FGR_MSG_TYPE_NULL message as a replay signal
            queue_msg_t replay_msg = {0};
            replay_msg.header.type = htons(((uint16_t) FGR_MSG_TYPE_NULL) << 12);
            replay_msg.header.level = 0;
            replay_msg.body = NULL;
            replay_msg.body_length = 0;

            if (xQueueSend(context->queue, &replay_msg, pdMS_TO_TICKS(100)) != pdPASS) {
                ESP_LOGW(TAG, "Failed to queue replay request.");
            }
        }

        CONTEXT_UNLOCK(context->lock, "socket_reconnect_cb() log");
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialize logging.
int32_t fgr_log_init(const char *server_ip, uint16_t port,
                     fgr_log_level_t level_min)
{
    int32_t err = ESP_OK;

    if (g_context.sock < 0) {
        if (!g_context.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_context.lock = xSemaphoreCreateMutex();
        }
    }

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_init()");

        if (!g_context.running) {

            g_context.level_min = level_min;

            // Read values from non-volatile storage and,
            // if not present, write the default value back
            if (nvs_on_not_off_get(&g_context.on_not_off) != ESP_OK) {
                nvs_on_not_off_set(g_context.on_not_off);
            }
            if (nvs_level_min_get(&g_context.level_min) != ESP_OK) {
                nvs_level_min_set(g_context.level_min);
            }

            esp_log_level_set("*", fgr_to_esp_log_level(g_context.level_min));

            // Create connection to server
            err = fgr_socket_channel_start(server_ip, port,
                                           &g_context.sock,
                                           &g_context.context_sock);
            if (err == ESP_OK) {

                xSemaphoreGive(g_context.lock);
                // Do initial extra socket configuration
                socket_reconnect_cb(g_context.sock, &g_context);
                xSemaphoreTake(g_context.lock, pdMS_TO_TICKS(1000));

                // Maintain the connection
                err = fgr_socket_channel_maintain(&g_context.context_sock,
                                                  CONFIG_FGR_LOG_HEARTBEAT_SECONDS,
                                                  socket_heartbeat_cb,
                                                  socket_reconnect_cb,
                                                  NULL,
                                                  &g_context);
                if (err == ESP_OK) {
                    g_context.connected = true;
                } else {
                    fgr_socket_channel_stop(&g_context.context_sock);
                    g_context.sock = -1;
                }
            }

            if (err == ESP_OK) {
                err = -ESP_ERR_NO_MEM;
                // Start logging queue and task
                if (!g_context.queue) {
                    g_context.queue = xQueueCreate(LOG_QUEUE_LENGTH, sizeof(queue_msg_t));
                    if (g_context.queue) {
                        // Initialize the log buffer
                        err = log_buffer_init(&g_context.buffer);
                        if (err != ESP_OK) {
                            ESP_LOGW(TAG, "No disconnect buffer (%s).", esp_err_to_name(err));
                            // Continue without it
                            err = ESP_OK;
                        }
                        g_context.running = true;
                        if (xTaskCreate(&task_log, "log", FGR_MSG_TASK_LOG_STACK_SIZE,
                                        &g_context, 5, &g_context.task) == pdPASS) {
                            err = ESP_OK;
                        } else {
                            g_context.running = false;
                            vQueueDelete(g_context.queue);
                            g_context.queue = NULL;
                        }
                    }
                }
            }

            if (err == ESP_OK) {
                // Set vprintf handler
                esp_log_set_vprintf(tcp_log_vprintf);
                ESP_LOGI(TAG, "Logs will be forwarded to %s:%d, log level %d.",
                        server_ip, port, g_context.level_min);

            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_init()");
    }

    if (err != ESP_OK) {
        clean_up();
    }

    return err;
}

// Deinitialise logging
void fgr_log_deinit(void)
{
    clean_up();
}

// Set minimum log level to forward
int32_t fgr_log_set_level_min(fgr_log_level_t level)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_set_level_min()");

        g_context.level_min = level;
        esp_log_level_set("*", fgr_to_esp_log_level(level));
        nvs_level_min_set(level);
        err = ESP_OK;

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_set_level_min()");

        ESP_LOGI(TAG, "Log level set to %d.", g_context.level_min);
    }

    return err;
}

// Stop logging.
int32_t fgr_log_off()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_off()");

        g_context.on_not_off = false;
        nvs_on_not_off_set(false);
        err = ESP_OK;

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_off()");

        ESP_LOGI(TAG, "Logging turned off.");
    }

    return err;
}

// Turn logging back on.
int32_t fgr_log_on()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_log_on()");

        g_context.on_not_off = true;
        nvs_on_not_off_set(true);
        err = ESP_OK;

        CONTEXT_UNLOCK(g_context.lock, "fgr_log_on()");

        ESP_LOGI(TAG, "Logging turned on.");
    }

    return err;
}

// A message receive callback.
bool fgr_log_msg_receive_cb(fgr_msg_t *msg, void *param)
{
    bool handled = false;
    uint32_t length = 0;
    // Only need two bytes for the stuff we return here
    uint8_t contents[2];

    (void) param;

    fgr_error_t msg_error = FGR_ERROR_UNHANDLED_REQUEST;

    if (IS_MSG_REQ(msg->header.req.type)) {
        // REQUEST messages
        handled = true;
        switch (MSG_MASK(msg->header.req.type)) {
            case FGR_REQ_CNF_LOG_LEVEL:
                //  Message contents should be a uint8_t that is fgr_log_level_t
                msg_error = FGR_ERROR_INVALID_PARAM;
                if (msg->body.length == 1) {
                    msg_error = FGR_ERROR_GENERIC;
                    fgr_log_level_t level = msg->body.contents[0];
                    if (fgr_log_set_level_min(level) == ESP_OK) {
                        msg_error = FGR_ERROR_NONE;
                    }
                }
            break;
            case FGR_REQ_CNF_LOG_START:
                msg_error = FGR_ERROR_GENERIC;
               if (fgr_log_on() == ESP_OK) {
                    msg_error = FGR_ERROR_NONE;
                }
            break;
            case FGR_REQ_CNF_LOG_STOP:
                msg_error = FGR_ERROR_GENERIC;
                if (fgr_log_off() == ESP_OK) {
                    msg_error = FGR_ERROR_NONE;
                }
            break;
            case FGR_REQ_CNF_LOG_STATUS:
                // Contents should be one uint8_t
                // representing the bool of log
                // on/off and another representing
                // the log level
                CONTEXT_LOCK(g_context.lock, "fgr_log_msg_receive_cb()");
                contents[0] = g_context.on_not_off;
                contents[1] = g_context.level_min;
                length = 2;
                CONTEXT_UNLOCK(g_context.lock, "fgr_log_msg_receive_cb()");
                msg_error = FGR_ERROR_NONE;
            break;
            default:
                handled = false;
            break;
        }

        if (handled) {
            fgr_msg_send_queue_cnf(MSG_MASK(msg->header.req.type), msg_error,
                                   msg->header.req.reference, contents, length);
        }
    }

    if (handled) {
        // This will be printed before the queued CNF message is sent
        fgr_msg_print_summary("Handled", FGR_LOG_LEVEL_INFO, msg->header.req.type, 0,
                              msg->header.req.reference, msg->body.length);
    }

    return handled;
}

// End of file

