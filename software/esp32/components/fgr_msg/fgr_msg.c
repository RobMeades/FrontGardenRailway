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
 * @brief Implementation of the messaging interface for a node of the
 * front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_log.h"
#include "arpa/inet.h"
#include "sys/queue.h"

#include "fgr_util.h"
#include "fgr_monitor.h"
#include "fgr_task.h"
#include "fgr_debug.h"
#include "fgr_metrics.h"
#include "fgr_socket.h"
#include "fgr_msg.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix.
#define TAG "msg"

// String to use to describe an unknown message, error value or state.
#define MSG_UNKNOWN_STR "UNKNOWN"

#ifndef FGR_MSG_TASK_SEND_QUEUE_STACK_SIZE
#  define FGR_MSG_TASK_SEND_QUEUE_STACK_SIZE (1024 * 4)
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// State for message decoder.
typedef struct {
    enum {
        DECODER_STATE_HEADER,
        DECODER_STATE_LENGTH,
        DECODER_STATE_CONTENTS,
        DECODER_STATE_COMPLETE
    } state;
    uint8_t header_buffer[sizeof(fgr_msg_header_t)];
    uint8_t length_buffer[sizeof(uint32_t)];
    size_t header_bytes_read;
    size_t length_bytes_read;
    size_t contents_bytes_read;
    uint32_t expected_contents_length;
    fgr_msg_t *msg;  // Will be allocated when length is known
} context_decoder_t;

// Structure to contain a message that is to be sent.
typedef struct {
    fgr_msg_type_t type;
    uint16_t msg_sub_type;
    uint8_t error;   // error for CNF messages, ignored for IND messages (which retrieve state via callback)
    uint8_t reference;   // only for CNF messages, ignored for IND messages (which just use the next reference)
    size_t body_length;
    void *body_contents;
} msg_send_t;

// Structure to store a message receive callback and its parameter
// as part of a linked list
typedef struct msg_rx_handler_cb_t {
    fgr_msg_receive_handler_cb_t cb;
    void *cb_param;
    uint16_t msg_type;
    SLIST_ENTRY(msg_rx_handler_cb_t) next;
} msg_rx_handler_cb_t;

// Message receive callback list head.
SLIST_HEAD(msg_rx_handler_cb_list_t, msg_rx_handler_cb_t);

// Context for message send queue.
typedef struct {
    SemaphoreHandle_t lock;
    TaskHandle_t task_send_handle;
    QueueHandle_t queue_send;
} context_send_t;

// Context.
typedef struct {
    int sock;
    void *context_sock;
    bool connected;
    SemaphoreHandle_t lock;
    uint8_t ind_reference;
    fgr_msg_state_cb_t state_cb;
    void *state_cb_param;
    fgr_msg_rssi_cb_t rssi_cb;
    void *rssi_cb_param;
    fgr_msg_send_cb_t send_cb;
    void *send_cb_param;
    fgr_msg_receive_cb_t receive_cb;
    void *receive_cb_param;
    fgr_msg_send_ping_body_cb_t send_ping_body_cb;
    void *send_ping_body_cb_param;
    void *context_rx;
    struct msg_rx_handler_cb_list_t msg_rx_handler_cb_list;
    context_decoder_t context_decoder;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// List of known message variety names, in order.
static const char *g_msg_variety_str_list[] = {"NULL", "REQ", "CNF", "IND", "RSP", "LOG"};

// List of known message REQ/CNF names, in order.
static const char *g_msg_req_cnf_str_list[] = {"NULL", "CFG", "START", "STOP",
                                               "LOG_LEVEL", "LOG_START", "LOG_STOP",
                                               "LOG_STATUS", "DEBUG_LED_OFF", "DEBUG_LED_ON",
                                               "DEBUG_LED_BREATHE_OFF", "DEBUG_LED_BREATHE_ON",
                                               "DEBUG_LED_STATUS", "REBOOT", "PING"};

// List of known message IND/RSP names, in order.
static const char *g_msg_ind_rsp_str_list[] = {"NULL", "NEEDS_CFG", "START", "STOP",
                                               "HEARTBEAT"};

// List of known message error codes, in order.
static const char *g_msg_error_str_list[] = {"NONE", "GENERIC", "INVALID_REQUEST",
                                             "UNHANDLED_REQUEST", "MSG_TOO_LONG",
                                             "ABORTED", "BUSY", "TIMEOUT",
                                             "OUT_OF_RESOURCES", "HARDWARE"};
// List of known message states, in order.
static const char *g_msg_state_str_list[] = {"NOT_POPULATED", "NEEDS_CFG", "STARTED",
                                             "STOPPED", "BUSY", "GENERIC_FAILED",
                                             "HARDWARE_FAILURE"};

// Send queue context.
static context_send_t g_context_send = {0};

// Main context.
static context_t g_context = {
    .sock = -1
};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MESSAGE DECODING
 * -------------------------------------------------------------- */

// Initialize a message decoder.
static void decoder_init(context_decoder_t *decoder)
{
    decoder->state = DECODER_STATE_HEADER;
    decoder->header_bytes_read = 0;
    decoder->length_bytes_read = 0;
    decoder->contents_bytes_read = 0;
    decoder->expected_contents_length = 0;
    decoder->msg = NULL;
}

// Free a decoder.
static void decoder_free(context_decoder_t *decoder)
{
    if (decoder->msg) {
        free(decoder->msg);
        decoder->msg = NULL;
    }
}

// Decode incoming data into a complete message: *msg will
// be non-NULL when a complete message is decoded.
// Note: When a complete message is returned, the caller is
// responsible for free()ing it.
// Note: this decoder written by Deep Seek, and hence has
// multiple return statements, which is OK 'cos it's a parser.
static int32_t decode_msg(context_decoder_t *decoder, const uint8_t *buffer,
                          size_t length, fgr_msg_t **msg)
{

    size_t bytes_processed = 0;
    *msg = NULL;

    while ((bytes_processed < length) && (decoder->state != DECODER_STATE_COMPLETE)) {

        switch (decoder->state) {
            case DECODER_STATE_HEADER: {
                // Calculate how many bytes we need to complete the header
                size_t bytes_needed = sizeof(fgr_msg_header_t) - decoder->header_bytes_read;
                size_t bytes_to_copy = (length - bytes_processed) < bytes_needed ?
                                       (length - bytes_processed) : bytes_needed;

                memcpy(decoder->header_buffer + decoder->header_bytes_read,
                       buffer + bytes_processed,
                       bytes_to_copy);

                decoder->header_bytes_read += bytes_to_copy;
                bytes_processed += bytes_to_copy;

                // Check if header is complete
                if (decoder->header_bytes_read == sizeof(fgr_msg_header_t)) {
                    decoder->state = DECODER_STATE_LENGTH;
                    decoder->length_bytes_read = 0;
                }
                break;
            }
            case DECODER_STATE_LENGTH: {
                // Calculate how many bytes we need to complete the length field
                size_t bytes_needed = sizeof(uint32_t) - decoder->length_bytes_read;
                size_t bytes_to_copy = (length - bytes_processed) < bytes_needed ?
                                       (length - bytes_processed) : bytes_needed;

                memcpy(decoder->length_buffer + decoder->length_bytes_read,
                       buffer + bytes_processed,
                       bytes_to_copy);

                decoder->length_bytes_read += bytes_to_copy;
                bytes_processed += bytes_to_copy;

                // Check if length field is complete
                if (decoder->length_bytes_read == sizeof(uint32_t)) {
                    // Extract the length with endianness conversion
                    decoder->expected_contents_length = ntohl(*((uint32_t*)decoder->length_buffer));

                    // Validate length (prevent crazy allocations)
                    if (decoder->expected_contents_length > FGR_MSG_CONTENTS_MAX_LEN) {
                        // Invalid length - reset decoder
                        decoder_init(decoder);
                        return -1;  // Error
                    }

                    // Allocate message structure with variable-length contents
                    // We allocate the header plus enough space for the body
                    size_t message_size = sizeof(fgr_msg_header_t) +
                                          sizeof(uint32_t) +  // length field
                                          decoder->expected_contents_length;

                    decoder->msg = (fgr_msg_t *) malloc(message_size);
                    if (!decoder->msg) {
                        decoder_init(decoder);
                        return -1;  // Out of memory
                    }

                    // Copy the header we already received
                    memcpy(&decoder->msg->header, decoder->header_buffer, sizeof(fgr_msg_header_t));

                    // Convert the type field (16-bit) from network to host byte order
                    // We can access via any of the union members since they all start with type
                    decoder->msg->header.req.type = ntohs(decoder->msg->header.req.type);

                    // Set the body length
                    decoder->msg->body.length = decoder->expected_contents_length;

                    if (decoder->expected_contents_length == 0) {
                        // No contents - message is complete
                        *msg = decoder->msg;
                        decoder->msg = NULL;
                        decoder_init(decoder);
                        return bytes_processed;
                    }

                    decoder->state = DECODER_STATE_CONTENTS;
                    decoder->contents_bytes_read = 0;
                }
                break;
            }
            case DECODER_STATE_CONTENTS: {
                size_t bytes_needed = decoder->expected_contents_length - decoder->contents_bytes_read;
                size_t bytes_to_copy = (length - bytes_processed) < bytes_needed ?
                                       (length - bytes_processed) : bytes_needed;
                memcpy(decoder->msg->body.contents + decoder->contents_bytes_read,
                       buffer + bytes_processed,
                       bytes_to_copy);

                decoder->contents_bytes_read += bytes_to_copy;
                bytes_processed += bytes_to_copy;

                // Check if all contents have been received
                if (decoder->contents_bytes_read == decoder->expected_contents_length) {
                    decoder->state = DECODER_STATE_COMPLETE;
                    *msg = decoder->msg;
                    decoder->msg = NULL;
                    decoder_init(decoder);
                    return bytes_processed;
                }
                break;
            }
            default:
                break;
        }
    }

    return bytes_processed;
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: SENDING
 * -------------------------------------------------------------- */

// Core of send_msg().
// IMPORTANT the context MUST be locked before this is called.
static int32_t send_msg_locked(context_t *context,
                               uint16_t type, uint8_t error_state,
                               uint8_t reference,
                               const void *buffer,
                               size_t length)
{
    fgr_msg_header_t *_header;
    uint32_t *_length;
    uint32_t header_and_length[(sizeof(*_header) + sizeof(*_length)) / sizeof(uint32_t)] = {0};

    // Assemble all of the bits
    _header = (fgr_msg_header_t *) & (header_and_length[0]);
    _length = &(header_and_length[sizeof(*_header) / sizeof(uint32_t)]);
    // We can just fill in the CNF bit of the union, the
    // IND part follows the same pattern
    _header->cnf.type = htons(type);
    _header->cnf.error = error_state;
    _header->cnf.reference = reference;
    if (buffer != NULL) {
        *_length = htonl(length);
    }

    // Send header and length
    int32_t total_bytes = sizeof(header_and_length);
    int32_t err = fgr_socket_send(context->sock, &header_and_length, sizeof(header_and_length),
                                  FGR_SOCKET_TX_RETRY_COUNT);
    if ((err == ESP_OK) && (buffer != NULL)) {
        // Send contents
        total_bytes += length;
        err = fgr_socket_send(context->sock, buffer, length, FGR_SOCKET_TX_RETRY_COUNT);
    }
    if (err == ESP_OK) {
        fgr_msg_print_summary(NULL, FGR_LOG_LEVEL_INFO, type, error_state, reference, length);
        fgr_socket_channel_activity(&context->context_sock);
        if (context->send_cb) {
            context->send_cb(context->send_cb_param);
        }
    } else {
        fgr_socket_channel_failed(&context->context_sock);
    }
    fgr_metrics_event_bool_add(FGR_METRIC_EVENT_BOOL_CONTROLLER_SOCKET_TX, err == ESP_OK, total_bytes);

    return err;
}

// Send a CNF or IND message.
static int32_t send_msg(uint16_t type, uint8_t error_state,
                        uint8_t reference, const uint8_t *buffer,
                        size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {
        err = -ESP_ERR_INVALID_ARG;
        if ((length == 0) || (buffer != NULL)) {

            CONTEXT_LOCK(g_context.lock, "send_msg()");
            err = send_msg_locked(&g_context, type, error_state, reference,
                                  buffer, length);
            CONTEXT_UNLOCK(g_context.lock, "send_msg()");
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLBACKS
 * -------------------------------------------------------------- */

// Send messages that were put on the send queue.
static void task_send_queue_cb(void *handle, void *param)
{
    context_send_t *context = (context_send_t *) param;
    msg_send_t msg;

    (void) handle;

    if (xQueueReceive(context->queue_send, &msg, pdMS_TO_TICKS(100)) == pdPASS) {
        if (msg.type == FGR_MSG_TYPE_CNF) {
            fgr_msg_send_cnf(msg.msg_sub_type, msg.error, msg.reference,
                             msg.body_contents, msg.body_length);
            free(msg.body_contents);
        } else if (msg.type == FGR_MSG_TYPE_IND) {
            fgr_msg_send_ind(msg.msg_sub_type, msg.body_contents, msg.body_length);
            free(msg.body_contents);
        } else {
            ESP_LOGE(TAG, "Unknown message type on send queue (%d)!", msg.type);
        }
    }
}

// Callback to send a heartbeat message.
static void socket_heartbeat_cb(int sock, void *param)
{
    context_t *context = (context_t *) param;
    uint8_t contents = 0;
    uint32_t length = 0;

    CONTEXT_LOCK(context->lock, "socket_heartbeat_cb() msg");
    if (context->rssi_cb) {
        // Populate a single byte in the contents containing the RSSI
        contents = (uint8_t) context->rssi_cb(context->rssi_cb_param);
        length++;
    }
    CONTEXT_UNLOCK(context->lock, "socket_heartbeat_cb() msg");

    fgr_msg_send_ind(FGR_IND_RSP_HEARTBEAT, &contents, length);
}

// Callback called by fgr_socket_channel_maintain() when a
// connection has been re-made.
static void socket_reconnect_cb(int sock, void *param)
{
    context_t *context = (context_t *) param;

    if (context->lock) {

        CONTEXT_LOCK(context->lock, "socket_reconnect_cb() msg");

        int32_t err = fgr_socket_enable_tcp_keep_alive(sock,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_COUNT);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "fgr_socket_enable_tcp_keep_alive() returned error: %s.", esp_err_to_name(err));
        }
        err = fgr_socket_enable_tcp_no_delay(sock);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "fgr_socket_enable_tcp_no_delay() returned error: %s.", esp_err_to_name(err));
        }
        fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_CONTROLLER_CONNECTION, true, 0);

        context->sock = sock;
        context->connected = true;
        // Set the debug LED back to normal
        fgr_debug_led_breathe_set(FGR_DEBUG_LED_COLOUR_NONE);

        CONTEXT_UNLOCK(context->lock, "socket_reconnect_cb() msg");
    }
}

// Callback called by fgr_socket_channel_maintain() when a
// connection has gone down.
static void socket_down_cb(void *param)
{
    (void) param;

    // Make the debug LED breathe an appropriate colour
    fgr_debug_led_breathe_set(FGR_DEBUG_LED_COLOUR_BAD);
    // Log the metric
    fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_CONTROLLER_CONNECTION, false, 0);
}

// Handle a ping message, called by receive_cb().
// IMPORTANT the context MUST be locked before this is called.
static void receive_handler_ping_locked(context_t *context, uint8_t reference)
{
    uint8_t *buffer = (uint8_t *) malloc(FGR_MSG_CONTENTS_MAX_LEN);
    uint32_t length = 0;

    if (buffer) {
        fgr_metrics_event_set(FGR_METRIC_EVENT_PING_RX, 0);

        // Populate the body
        if (context->send_ping_body_cb) {
            // Application wants to populate the body: let them do it
            length = context->send_ping_body_cb(buffer, FGR_MSG_CONTENTS_MAX_LEN,
                                                context->send_ping_body_cb_param);
        } else {
            if (context->state_cb) {
                // Populate the body with a uint8_t containing the state
                buffer[0] = context->state_cb(context->state_cb_param);
                length = sizeof(uint8_t);
            }
        }

        // Send the FGR_REQ_CNF_PING message
        send_msg_locked(context, MSG_CNF(FGR_REQ_CNF_PING), FGR_ERROR_NONE,
                        reference, buffer, length);

        free(buffer);
    }
}

// Callback to be called when data has been received.
static void receive_cb(void *buffer, size_t length, void *param)
{
    context_t *context = (context_t *) param;

    if (context->lock && buffer && (length > 0)) {

        fgr_metrics_event_add(FGR_METRIC_EVENT_CONTROLLER_SOCKET_RX, length);

        CONTEXT_LOCK(context->lock, "receive_cb()");
        fgr_msg_t *msg = NULL;
        decode_msg(&context->context_decoder, (uint8_t *) buffer, length, &msg);
        if (msg != NULL) {
            char buffer_str[64] = {0};
            fgr_msg_name(msg->header.req.type, buffer_str, sizeof(buffer_str));
            ESP_LOGD(TAG, "Received %s [0x%04x], reference %d, body length %d.",
                     buffer_str, msg->header.req.type, msg->header.req.reference,
                     msg->body.length);

            // Got a complete message, ping whoever wanted to know we're connected
            if (context->receive_cb) {
                context->receive_cb(context->receive_cb_param);
            }
            // Then pass it to all who want it
            struct msg_rx_handler_cb_t *iter;
            bool handled = false;
            SLIST_FOREACH(iter, &context->msg_rx_handler_cb_list, next) {
                if ((iter->msg_type == 0) ||
                        (iter->msg_type == msg->header.req.type)) {
                    handled = iter->cb(msg, iter->cb_param);
                    if (handled) {
                        // Stop if the callback returns true
                        break;
                    }
                }
            }
            if (!handled) {
                if (MSG_MASK(msg->header.req.type) == FGR_IND_RSP_HEARTBEAT) {
                    // Just absorb these
                } else if (MSG_MASK(msg->header.req.type) == FGR_REQ_CNF_PING) {
                    // If a ping request has not been handled, handle
                    // it automatically
                    receive_handler_ping_locked(context,
                                                msg->header.req.reference);
                } else {
                    fgr_msg_print_summary("Unhandled message",
                                          FGR_LOG_LEVEL_WARN,
                                          msg->header.req.type, 0,
                                          msg->header.req.reference,
                                          msg->body.length);
                }
            }
            free(msg);
        }
        fgr_socket_channel_activity(&context->context_sock);
        CONTEXT_UNLOCK(context->lock, "receive_cb()");
    }
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Clean up.
static void clean_up()
{
    // Stop receiving
    fgr_msg_receive_stop();

    // Stop sending from a queue
    fgr_msg_send_queue_deinit();

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "clean_up() msg");

        // Lose the socket
        fgr_socket_channel_stop(&g_context.context_sock);
        g_context.sock = -1;
        g_context.state_cb = NULL;
        g_context.state_cb_param = NULL;
        g_context.rssi_cb = NULL;
        g_context.rssi_cb_param = NULL;
        g_context.send_cb = NULL;
        g_context.send_cb_param = NULL;
        g_context.receive_cb = NULL;
        g_context.receive_cb_param = NULL;

        // In case we were in the middle of a decode
        decoder_free(&g_context.context_decoder);

        CONTEXT_UNLOCK(g_context.lock, "clean_up() msg");
        // Don't delete the semaphore, someone might have it still
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: INITIALISATION/DEINITIALISATION
 * -------------------------------------------------------------- */

// Initialise the messaging interface.
int32_t fgr_msg_init(const char *server_ip, uint16_t port,
                     size_t heartbeat_seconds, fgr_msg_state_cb_t cb,
                     void *cb_param)
{
    int32_t err = ESP_OK;

    if (g_context.sock < 0) {
        if (!g_context.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_context.lock = xSemaphoreCreateMutex();
            SLIST_INIT(&g_context.msg_rx_handler_cb_list);
        }

        if (g_context.lock) {

            err = ESP_OK;

            CONTEXT_LOCK(g_context.lock, "fgr_msg_init()");

            if (g_context.sock < 0) {

                g_context.state_cb = cb;
                g_context.state_cb_param = cb_param;

                // Create connection to server
                err = fgr_socket_channel_start(server_ip, port,
                                               &g_context.sock,
                                               &g_context.context_sock);
                if (err == ESP_OK) {

                    fgr_metrics_event_bool_set(FGR_METRIC_EVENT_BOOL_CONTROLLER_CONNECTION,
                                               true, 0);

                    xSemaphoreGive(g_context.lock);
                    // Do initial extra socket configuration
                    socket_reconnect_cb(g_context.sock, &g_context);
                    xSemaphoreTake(g_context.lock, pdMS_TO_TICKS(1000));

                    // Maintain the connection
                    err = fgr_socket_channel_maintain(&g_context.context_sock,
                                                      heartbeat_seconds,
                                                      socket_heartbeat_cb,
                                                      socket_reconnect_cb,
                                                      socket_down_cb,
                                                      &g_context);
                    if (err != ESP_OK) {
                        fgr_socket_channel_stop(&g_context.context_sock);
                        g_context.sock = -1;
                        g_context.state_cb = NULL;
                        g_context.state_cb_param = NULL;
                    }
                }
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_msg_init()");
        }
    }

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Connected to controller.");
    } else {
        clean_up();
    }

    return err;
}

// Deinitialise the messaging interface.
void fgr_msg_deinit()
{
    clean_up();
}

// Set a callback that will return an RSSI reading.
int32_t fgr_msg_rssi_cb_set(fgr_msg_rssi_cb_t cb, void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_rssi_cb()");
        g_context.rssi_cb = cb;
        g_context.rssi_cb_param = cb_param;
        err = ESP_OK;
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_rssi_cb()");
    }

    return err;
}

// Determine if messaging is connected to the controller.
bool fgr_msg_is_connected()
{
    bool is_connected = false;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_is_connected()");
        is_connected = g_context.connected;
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_is_connected()");
    }

    return is_connected;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: SENDING
 * -------------------------------------------------------------- */

// Send a CNF message.
int32_t fgr_msg_send_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
                         uint8_t reference, const void *buffer,
                         size_t length)
{
    cnf = MSG_CNF(cnf);
    return send_msg((uint16_t) cnf, (uint8_t) error, reference, buffer, length);
}

// Send an IND message.
int32_t fgr_msg_send_ind(fgr_ind_rsp_t ind, const void *buffer,
                         size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        fgr_state_t state = FGR_STATE_NOT_POPULATED;
        uint8_t reference;
        ind = MSG_IND(ind);

        CONTEXT_LOCK(g_context.lock, "fgr_msg_send_ind()");
        if (g_context.state_cb) {
            state = g_context.state_cb(g_context.state_cb_param);
        }
        reference = g_context.ind_reference;
        g_context.ind_reference++;
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_send_ind()");

        err = send_msg((uint16_t) ind, (uint8_t) state, reference,
                       (const uint8_t *) buffer, length);
    }

    return err;
}

// Initialise a send message task and queue.
int32_t fgr_msg_send_queue_init(size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (!g_context_send.lock) {
        // Create mutex
        err = -ESP_ERR_NO_MEM;
        g_context_send.lock = xSemaphoreCreateMutex();
    }
    if (g_context_send.lock) {

        err = -ESP_ERR_NO_MEM;
        CONTEXT_LOCK(g_context_send.lock, "fgr_msg_send_queue_init()");
        if (!g_context_send.queue_send) {
            g_context_send.queue_send = xQueueCreate(length, sizeof(msg_send_t));
            if (g_context_send.queue_send) {
                err = fgr_task_create(&task_send_queue_cb, &g_context_send,
                                      "send_queue",
                                      FGR_MSG_TASK_SEND_QUEUE_STACK_SIZE,
                                      5, &g_context_send.task_send_handle);
                if (err != ESP_OK) {
                    vQueueDelete(g_context_send.queue_send);
                    g_context_send.queue_send = NULL;
                }
            }
        }
        CONTEXT_UNLOCK(g_context_send.lock, "fgr_msg_send_queue_init()");
    }

    return err;
}

// Queue a CNF message.
int32_t fgr_msg_send_queue_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
                               uint8_t reference, const void *buffer,
                               size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context_send.lock) {

        CONTEXT_LOCK(g_context_send.lock, "fgr_msg_send_queue_cnf()");
        if (g_context_send.queue_send) {
            err = -ESP_ERR_NO_MEM;
            msg_send_t msg_send = {0};
            msg_send.type = FGR_MSG_TYPE_CNF;
            msg_send.msg_sub_type = cnf;
            msg_send.error = error;
            msg_send.reference = reference;
            msg_send.body_length = length;
            if (length > 0) {
                msg_send.body_contents = malloc(length);
                if (msg_send.body_contents) {
                    memcpy(msg_send.body_contents, buffer, length);
                }
            }
            if ((length == 0) || msg_send.body_contents) {
                if (xQueueSend(g_context_send.queue_send, &msg_send, 0) == pdTRUE) {
                    err = ESP_OK;
                } else {
                    free(msg_send.body_contents);
                }
            }
        }
        CONTEXT_UNLOCK(g_context_send.lock, "fgr_msg_send_queue_cnf()");
    }

    if (err != ESP_OK) {
        char buffer[64];
        cnf = MSG_CNF(cnf);
        fgr_msg_name(cnf, buffer, sizeof(buffer));
        ESP_LOGE(TAG, "Unable to queue CNF message %s [0x%04x], body length %d: %s.",
                 buffer, esp_err_to_name(err));
    }

    return err;
}

// Queue an IND message.
int32_t fgr_msg_send_queue_ind(fgr_ind_rsp_t ind, const void *buffer,
                               size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context_send.lock) {

        CONTEXT_LOCK(g_context_send.lock, "fgr_msg_send_queue_ind()");
        if (g_context_send.queue_send) {
            err = -ESP_ERR_NO_MEM;
            msg_send_t msg_send = {0};
            msg_send.type = FGR_MSG_TYPE_IND;
            msg_send.msg_sub_type = ind;
            msg_send.body_length = length;
            if (length > 0) {
                msg_send.body_contents = malloc(length);
                if (msg_send.body_contents) {
                    memcpy(msg_send.body_contents, buffer, length);
                }
            }
            if ((length == 0) || msg_send.body_contents) {
                if (xQueueSend(g_context_send.queue_send, &msg_send, 0) == pdTRUE) {
                    err = ESP_OK;
                } else {
                    free(msg_send.body_contents);
                }
            }
        }
        CONTEXT_UNLOCK(g_context_send.lock, "fgr_msg_send_queue_ind()");
    }

    if (err != ESP_OK) {
        char buffer[64];
        ind = MSG_IND(ind);
        fgr_msg_name(ind, buffer, sizeof(buffer));
        ESP_LOGE(TAG, "Unable to queue CNF message %s [0x%04x], body length %d: %s.",
                 buffer, esp_err_to_name(err));
    }

    return err;
}

// Get how many messages are currently on the send queue.
int32_t fgr_msg_send_queue_size()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context_send.lock) {

        err = ESP_OK;
        CONTEXT_LOCK(g_context_send.lock, "fgr_msg_send_queue_size()");
        if (g_context_send.queue_send) {
            err = uxQueueMessagesWaiting(g_context_send.queue_send);
        }
        CONTEXT_UNLOCK(g_context_send.lock, "fgr_msg_send_queue_size()");
    }

    return err;
}

// Destroy a send message queue.
void fgr_msg_send_queue_deinit()
{
    if (g_context_send.lock) {

        // Need to do this before taking the lock or we
        // will lock-up the task exit
        fgr_task_destroy(g_context_send.task_send_handle);
        g_context_send.task_send_handle = NULL;

        CONTEXT_LOCK(g_context_send.lock, "fgr_msg_send_queue_deinit()");
        if (g_context_send.queue_send) {
            msg_send_t msg;
            while (xQueueReceive(g_context_send.queue_send, &msg, 0) == pdTRUE) {
                free(msg.body_contents);
                vTaskDelay(pdMS_TO_TICKS(FGR_UTIL_WATCHDOG_FEED_TIME_MS));
            }
            vQueueDelete(g_context_send.queue_send);
            g_context_send.queue_send = NULL;
        }
        CONTEXT_UNLOCK(g_context_send.lock, "fgr_msg_send_queue_deinit()");
        // Don't delete the semaphore, someone might have it still
    }
}

// Set a callback to be called whenever a message is sent.
int32_t fgr_msg_send_cb_set(fgr_msg_send_cb_t cb, void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_send_cb()");
        g_context.send_cb = cb;
        g_context.send_cb_param = cb_param;
        err = ESP_OK;
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_send_cb()");
    }

    return err;
}

// Set a callback to populate the body of an FGR_REQ_CNF_PING message.
int32_t fgr_msg_send_ping_body_cb(fgr_msg_send_ping_body_cb_t cb,
                                  void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_send_ping_body_cb()");
        g_context.send_ping_body_cb = cb;
        g_context.send_ping_body_cb_param = cb_param;
        err = ESP_OK;
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_send_ping_body_cb()");
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: RECEIVING
 * -------------------------------------------------------------- */

// Start receiving messages.
int32_t fgr_msg_receive_start()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_receive_start()");
        decoder_init(&g_context.context_decoder);
        err = fgr_socket_receive_start(g_context.sock,
                                       fgr_socket_channel_failed,
                                       &g_context.context_sock,
                                       receive_cb,
                                       &g_context,
                                       &g_context.context_rx);
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_receive_start()");
    }

    return err;
}

// Set a callback for whenever a message is received.
int32_t fgr_msg_receive_cb_set(fgr_msg_receive_cb_t cb, void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_receive_cb_set()");
        g_context.receive_cb = cb;
        g_context.receive_cb_param = cb_param;
        err = ESP_OK;
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_receive_cb_set()");
    }

    return err;
}

// Add a message receive handler.
int32_t fgr_msg_receive_handler_add(uint16_t msg_type,
                                    fgr_msg_receive_handler_cb_t cb,
                                    void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_context.lock) {
        err = -ESP_ERR_INVALID_ARG;
        if (cb &&
            ((msg_type == 0) ||
             ((msg_type >> 12) == FGR_MSG_TYPE_REQ) ||
             ((msg_type >> 12) == FGR_MSG_TYPE_IND))) {
            err = -ESP_ERR_NO_MEM;
            msg_rx_handler_cb_t *msg_rx_cb = (msg_rx_handler_cb_t *) malloc(sizeof(*msg_rx_cb));
            if (msg_rx_cb) {
                msg_rx_cb->msg_type = msg_type;
                msg_rx_cb->cb = cb;
                msg_rx_cb->cb_param = cb_param;

                CONTEXT_LOCK(g_context.lock, "fgr_msg_receive_handler_add()");
                SLIST_INSERT_HEAD(&g_context.msg_rx_handler_cb_list, msg_rx_cb, next);
                CONTEXT_UNLOCK(g_context.lock, "fgr_msg_receive_handler_add()");

                err = ESP_OK;
            }
        }
    }

    return err;
}

// Remove a message receive handler.
void fgr_msg_receive_handler_remove_by_cb(fgr_msg_receive_handler_cb_t cb)
{
    if (g_context.lock && cb) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_receive_handler_remove_by_cb()");
        msg_rx_handler_cb_t *iter;
        msg_rx_handler_cb_t *prev = NULL;
        SLIST_FOREACH(iter, &g_context.msg_rx_handler_cb_list, next) {
            if (iter->cb == cb) {
                if (prev == NULL) {
                    // Removing the first element
                    SLIST_REMOVE_HEAD(&g_context.msg_rx_handler_cb_list, next);
                } else {
                    // Removing a middle element
                    SLIST_REMOVE_AFTER(prev, next);
                }
                free(iter);
                // Done; MUST break after an insertion or removal as
                // otherwise SLIST_FOREACH will go bang as it
                // relies on pointers still being valid.
                break;
            }
            prev = iter;
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_receive_handler_remove_by_cb()");
    }
}

// Remove a message receive handler.
void fgr_msg_receive_handler_remove_by_type(uint16_t msg_type)
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_receive_handler_remove_by_type()");
        msg_rx_handler_cb_t *iter;
        msg_rx_handler_cb_t *prev = NULL;
        SLIST_FOREACH(iter, &g_context.msg_rx_handler_cb_list, next) {
            if (iter->msg_type == msg_type) {
                if (prev == NULL) {
                    // Removing the first element
                    SLIST_REMOVE_HEAD(&g_context.msg_rx_handler_cb_list, next);
                } else {
                    // Removing a middle element
                    SLIST_REMOVE_AFTER(prev, next);
                }
                free(iter);
                // Done; MUST break after an insertion or removal as
                // otherwise SLIST_FOREACH will go bang as it
                // relies on pointers still being valid.
                break;
            }
            prev = iter;
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_receive_handler_remove_by_type()");
    }
}

// Stop receiving messages.
void fgr_msg_receive_stop()
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_msg_receive_stop()");
        fgr_socket_receive_stop(&g_context.context_rx);
        while (!SLIST_EMPTY(&g_context.msg_rx_handler_cb_list)) {
            msg_rx_handler_cb_t *p = SLIST_FIRST(&g_context.msg_rx_handler_cb_list);
            SLIST_REMOVE_HEAD(&g_context.msg_rx_handler_cb_list, next);
            free(p);
        }
        CONTEXT_UNLOCK(g_context.lock, "fgr_msg_receive_stop()");
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: DEBUG
 * -------------------------------------------------------------- */

// Populate a buffer with a string that is the name of the given message.
int32_t fgr_msg_name(uint16_t msg_type, char *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint8_t variety = msg_type >> 12;
    const char *variety_str = NULL;
    const char *msg_type_str = NULL;

    if (buffer && (length > 0)) {
        err = -ESP_ERR_NOT_FOUND;
        msg_type &= 0x0fff;
        if (variety < FGR_UTIL_ARRAY_LENGTH(g_msg_variety_str_list)) {
            variety_str = g_msg_variety_str_list[variety];
            if ((variety == FGR_MSG_TYPE_REQ) || (variety == FGR_MSG_TYPE_CNF)) {
                if (msg_type < FGR_UTIL_ARRAY_LENGTH(g_msg_req_cnf_str_list)) {
                    msg_type_str = g_msg_req_cnf_str_list[msg_type];
                }
            } else if ((variety == FGR_MSG_TYPE_IND) || (variety == FGR_MSG_TYPE_RSP)) {
                if (msg_type < FGR_UTIL_ARRAY_LENGTH(g_msg_ind_rsp_str_list)) {
                    msg_type_str = g_msg_ind_rsp_str_list[msg_type];
                }
            }
        }

        if (variety_str && msg_type_str) {
            err = snprintf(buffer, length, "FGR_%s_%s", variety_str, msg_type_str);
        } else {
            err = snprintf(buffer, length, "%s", MSG_UNKNOWN_STR);
        }

        // Ensure a null terminator and no overrun
        if (err >= (int32_t) length) {
            err = length - 1;
            buffer[length - 1] = 0;
        }
    }

    return err;
}

// Populate a buffer with a string that is the name of the given error.
int32_t fgr_msg_error_name(fgr_error_t error, char *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (buffer && (length > 0)) {
        const char *error_str = NULL;
        err = -ESP_ERR_NOT_FOUND;
        if (error < FGR_UTIL_ARRAY_LENGTH(g_msg_error_str_list)) {
            error_str = g_msg_error_str_list[error];
        }

        if (error_str) {
            err = snprintf(buffer, length, "FGR_ERROR_%s", error_str);
        } else {
            err = snprintf(buffer, length, "%s", MSG_UNKNOWN_STR);
        }

        // Ensure a null terminator and no overrun
        if (err >= (int32_t) length) {
            err = length - 1;
            buffer[length - 1] = 0;
        }
    }

    return err;
}

// Populate a buffer with a string that is the name of the given state.
int32_t fgr_msg_state_name(fgr_state_t state, char *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (buffer && (length > 0)) {
        const char *state_str = NULL;
        err = -ESP_ERR_NOT_FOUND;
        if (state < FGR_UTIL_ARRAY_LENGTH(g_msg_state_str_list)) {
            state_str = g_msg_state_str_list[state];
        }

        if (state_str) {
            err = snprintf(buffer, length, "FGR_STATE_%s", state_str);
        } else {
            err = snprintf(buffer, length, "%s", MSG_UNKNOWN_STR);
        }

        // Ensure a null terminator and no overrun
        if (err >= (int32_t) length) {
            err = length - 1;
            buffer[length - 1] = 0;
        }
    }

    return err;
}

// Print a summary of a message for debug purposes.
void fgr_msg_print_summary(const char *prefix_str, fgr_log_level_t level,
                           uint16_t msg_type, uint8_t error_state,
                           uint8_t reference, uint32_t length)
{
    char buffer_msg_name[64];
    char buffer_error_state[32];
    char buffer[256];

    fgr_msg_name(msg_type, buffer_msg_name, sizeof(buffer_msg_name));
    if (((msg_type >> 12) == FGR_MSG_TYPE_REQ) ||
        ((msg_type >> 12) == FGR_MSG_TYPE_RSP)) {
        if (prefix_str == NULL) {
            prefix_str = "Received";
        }
        snprintf(buffer, sizeof(buffer), "%s %s [0x%04x], reference %d, length %lu.",
                 prefix_str, buffer_msg_name, msg_type, reference, length);
    } else {
        if (prefix_str == NULL) {
            prefix_str = "Sent";
        }
        if ((msg_type >> 12) == FGR_MSG_TYPE_CNF) {
            fgr_msg_error_name(error_state, buffer_error_state, sizeof(buffer_error_state));
            snprintf(buffer, sizeof(buffer), "%s %s [0x%04x], error %s [%d], reference %d, length %lu.",
                     prefix_str, buffer_msg_name, msg_type, buffer_error_state, error_state, reference, length);
        } else if ((msg_type >> 12) == FGR_MSG_TYPE_IND) {
            fgr_msg_state_name(error_state, buffer_error_state, sizeof(buffer_error_state));
            snprintf(buffer, sizeof(buffer), "%s %s [0x%04x], state %s [%d], reference %d, length %lu.",
                     prefix_str, buffer_msg_name, msg_type, buffer_error_state, error_state, reference, length);
        } else {
            snprintf(buffer, sizeof(buffer), "Unknown message type (0x%04x).", msg_type);
        }
    }

    switch (level) {
        case FGR_LOG_LEVEL_DEBUG:
            ESP_LOGD(TAG, "%s", buffer);
            break;
        case FGR_LOG_LEVEL_WARN:
            ESP_LOGW(TAG, "%s", buffer);
            break;
        case FGR_LOG_LEVEL_ERROR:
            ESP_LOGE(TAG, "%s", buffer);
            break;
        case FGR_LOG_LEVEL_INFO:
        default:
            ESP_LOGI(TAG, "%s", buffer);
            break;
    }
}

// End of file
