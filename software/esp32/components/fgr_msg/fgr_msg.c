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

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_log.h"

#include "fgr_util.h"
#include "fgr_socket.h"
#include "fgr_msg.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix.
#define TAG "msg"

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
} msg_decoder_t;

// Context.
typedef struct {
    int sock;
    void *context_sock;
    bool connected;
    SemaphoreHandle_t lock;
    uint8_t reference;
    fgr_state_t *heartbeat_state;
    fgr_msg_rx_cb_t cb;
    void * cb_param;
    void *context_rx;
    msg_decoder_t msg_decoder;
} msg_cfg_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static msg_cfg_t g_msg_cfg = {
    .sock = -1
};

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Callback to send a heartbeat message.
static void socket_heartbeat_cb(int sock, void *param)
{
    msg_cfg_t *msg_cfg = (msg_cfg_t *) param;

    if (msg_cfg->lock) {

        fgr_state_t state = FGR_STATE_NOT_POPULATED;

        CONTEXT_LOCK(msg_cfg->lock, "socket_heartbeat_cb() 2");
        if (g_msg_cfg.heartbeat_state) {
            state = *g_msg_cfg.heartbeat_state;
        }
        CONTEXT_UNLOCK(msg_cfg->lock, "socket_heartbeat_cb() 2");

        fgr_msg_send_ind(FGR_IND_RSP_HEARTBEAT, state, NULL, 0);
    }
}

// Callback called by fgr_socket_channel_maintain().
static void socket_reconnect_cb(int sock, void *param)
{
    msg_cfg_t *msg_cfg = (msg_cfg_t *) param;

    if (msg_cfg->lock) {

        CONTEXT_LOCK(msg_cfg->lock, "socket_reconnect_cb() 2");
        int32_t err = fgr_socket_enable_tcp_keep_alive(sock,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_IDLE_TIME_SECONDS,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_PROBE_INTERVAL_SECONDS,
                                                       FGR_SOCKET_TCP_KEEP_ALIVE_COUNT);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "fgr_socket_enable_tcp_keep_alive() returned error: %s.", esp_err_to_name(err));
        }
        msg_cfg->sock = sock;
        msg_cfg->connected = true;
        CONTEXT_UNLOCK(msg_cfg->lock, "socket_reconnect_cb() 2");
    }
}

// Initialize a message decoder.
static void decoder_init(msg_decoder_t *decoder)
{
    decoder->state = DECODER_STATE_HEADER;
    decoder->header_bytes_read = 0;
    decoder->length_bytes_read = 0;
    decoder->contents_bytes_read = 0;
    decoder->expected_contents_length = 0;
    decoder->msg = NULL;
}

// Free a decoder.
static void decoder_free(msg_decoder_t *decoder)
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
static int32_t decode_msg(msg_decoder_t *decoder, const uint8_t *buffer,
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

// Callback to be called when data has been received.
static void receive_cb(void *buffer, size_t length, void *param)
{
    msg_cfg_t *msg_cfg = (msg_cfg_t *) param;

    if (msg_cfg->lock && buffer && (length > 0)) {

        CONTEXT_LOCK(msg_cfg->lock, "receive_cb()");
        fgr_msg_t *msg = NULL;
        decode_msg(&msg_cfg->msg_decoder, (uint8_t *) buffer, length, &msg);
        if (msg != NULL) {
            // Got a complete message, call the callback
            if (msg_cfg->cb != NULL) {
                msg_cfg->cb(msg, msg_cfg->cb_param);
            }
            free(msg);
        }
        fgr_socket_channel_activity(&msg_cfg->context_sock);
        CONTEXT_UNLOCK(msg_cfg->lock, "receive_cb()");
    }
}

// Send a CNF or IND message.
static int32_t send_msg(uint16_t type, uint8_t error_state,
                        const uint8_t *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_msg_cfg.lock) {
        err = -ESP_ERR_INVALID_ARG;
        if ((length == 0) || (buffer != NULL)) {

            fgr_msg_header_t *_header;
            uint32_t *_length;
            uint32_t header_length[(sizeof(*_header) + sizeof(*_length)) / sizeof(uint32_t)] = {0};
            _header = (fgr_msg_header_t *) &(header_length[0]);
            _length = &(header_length[sizeof(*_header) / sizeof(uint32_t)]);
            // We can just fill in the CNF bit of the union, the
            // IND part follows the same pattern
            _header->cnf.type = htons(type);
            _header->cnf.error = error_state;
            _header->cnf.reference = g_msg_cfg.reference;
            g_msg_cfg.reference++;
            if (buffer != NULL) {
                *_length = htonl(length);
            }

            CONTEXT_LOCK(g_msg_cfg.lock, "send_msg()");
            // Send header and length
            err = fgr_socket_send(g_msg_cfg.sock, &header_length, sizeof(header_length), FGR_SOCKET_TX_RETRY_COUNT);
            if ((err == ESP_OK) && (buffer != NULL)) {
                // Send contents
                err = fgr_socket_send(g_msg_cfg.sock, buffer, length, FGR_SOCKET_TX_RETRY_COUNT);
            }
            if (err == ESP_OK) {
                fgr_socket_channel_activity(&g_msg_cfg.context_sock);
            } else {
                fgr_socket_channel_failed(&g_msg_cfg.context_sock);
            }
            CONTEXT_UNLOCK(g_msg_cfg.lock, "send_msg()");
        }
    }

    return err;
}


// Clean up.
static void clean_up()
{
    if (g_msg_cfg.lock) {

        CONTEXT_LOCK(g_msg_cfg.lock, "clean_up() 2");

        // Lose the socket
        fgr_socket_channel_stop(&g_msg_cfg.context_sock);
        g_msg_cfg.sock = -1;

        // In case we were in the middle of a decode
        decoder_free(&g_msg_cfg.msg_decoder);

        CONTEXT_UNLOCK(g_msg_cfg.lock, "clean_up() 2");
        // Don't delete the semaphore, someone might have it still
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise the messaging interface.
int32_t fgr_msg_init(const char *server_ip, uint16_t port,
                     size_t hearbeat_seconds, fgr_state_t *state)
{
    int32_t err = ESP_OK;

    if (g_msg_cfg.sock < 0) {
        if (!g_msg_cfg.lock) {
            // Create mutex
            err = -ESP_ERR_NO_MEM;
            g_msg_cfg.lock = xSemaphoreCreateMutex();
        }

        if (g_msg_cfg.lock) {

            CONTEXT_LOCK(g_msg_cfg.lock, "fgr_msg_init()");

            if (hearbeat_seconds > 0) {
                g_msg_cfg.heartbeat_state = state;
            }

            // Create connection to server
            err = fgr_socket_channel_start(server_ip, port,
                                           &g_msg_cfg.sock,
                                           &g_msg_cfg.context_sock);
            if (err == ESP_OK) {

                CONTEXT_UNLOCK(g_msg_cfg.lock, "fgr_msg_init()");
                // Do initial extra socket configuration
                socket_reconnect_cb(g_msg_cfg.sock, &g_msg_cfg);
                CONTEXT_LOCK(g_msg_cfg.lock, "fgr_msg_init()");

                // Maintain the connection
                err = fgr_socket_channel_maintain(&g_msg_cfg.context_sock,
                                                  hearbeat_seconds,
                                                  socket_heartbeat_cb,
                                                  socket_reconnect_cb,
                                                  &g_msg_cfg);
                if (err != ESP_OK) {
                    fgr_socket_channel_stop(&g_msg_cfg.context_sock);
                    g_msg_cfg.sock = -1;
                    g_msg_cfg.heartbeat_state = NULL;
                }
            }

            CONTEXT_UNLOCK(g_msg_cfg.lock, "fgr_msg_init()");
        }
    }

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Connected to controller.");
    } else {
        clean_up();
    }

    return (int32_t) err;
}

// Send a CNF message.
int32_t fgr_msg_send_cnf(fgr_req_cnf_t cnf, fgr_error_t error,
                         const void *buffer, size_t length)
{
    cnf = (cnf & (0x0fff)) | (FGR_MSG_TYPE_CNF << 12);
    return send_msg((uint16_t) cnf, (uint8_t) error, buffer, length);
}

// Send an IND message.
int32_t fgr_msg_send_ind(fgr_ind_rsp_t ind, fgr_state_t state,
                         const void *buffer, size_t length)
{
    ind = (ind & (0x0fff)) | (FGR_MSG_TYPE_IND << 12);
    return send_msg((uint16_t) ind, (uint8_t) state, (const uint8_t *) buffer, length);
}

// Start receiving messages.
int32_t fgr_msg_receive_start(fgr_msg_rx_cb_t cb, void *cb_param)
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_msg_cfg.lock) {

        CONTEXT_LOCK(g_msg_cfg.lock, "fgr_msg_receive_start()");
        g_msg_cfg.cb = cb;
        g_msg_cfg.cb_param = cb_param;
        decoder_init(&g_msg_cfg.msg_decoder);
        err = fgr_socket_receive_start(g_msg_cfg.sock,
                                       fgr_socket_channel_failed,
                                       &g_msg_cfg.context_sock,
                                       receive_cb,
                                       &g_msg_cfg,
                                       &g_msg_cfg.context_rx);
        if (err != ESP_OK) {
            g_msg_cfg.cb = NULL;
            g_msg_cfg.cb_param = NULL;
        }
        CONTEXT_UNLOCK(g_msg_cfg.lock, "fgr_msg_receive_start()");
    }

    return err;
}

// Stop receiving messages.
void fgr_msg_receive_stop()
{
    if (g_msg_cfg.lock) {

        CONTEXT_LOCK(g_msg_cfg.lock, "fgr_msg_receive_stop()");
        fgr_socket_receive_stop(&g_msg_cfg.context_rx);
        g_msg_cfg.cb = NULL;
        g_msg_cfg.cb_param = NULL;
        CONTEXT_UNLOCK(g_msg_cfg.lock, "fgr_msg_receive_stop()");
    }
}

// Deinitialise the messaging interface.
void fgr_msg_deinit()
{
    clean_up();
}

// End of file

