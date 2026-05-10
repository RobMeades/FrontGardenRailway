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
 * @brief Implementation of an OTA-updated level gauge node for the
 * front garden railway.
 */

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "esp_event.h"
#include "arpa/inet.h"
#include "esp_log.h"
#include "errno.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"

#include "../../../../protocol/fgr_protocol.h"
#include "fgr_util.h"
#include "fgr_ota.h"
#include "fgr_network.h"
#include "fgr_socket.h"
#include "fgr_msg.h"
#include "fgr_debug.h"
#include "fgr_log.h"

#include "fgr_rcwl9610a.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix.
 #define TAG "level_gauge"

// The default reporting interval in seconds.
#define REPORTING_INTERVAL_DEFAULT_SECONDS 1

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Context.
typedef struct {
    fgr_state_t state;
    SemaphoreHandle_t lock;
    bool running;
    uint32_t reporting_interval_seconds;
} context_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// The CA certificate for the OTA update server.
extern const uint8_t g_server_cert_pem_start[] asm("_binary_ca_cert_pem_start");

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Set the node's state.
// IMPORTANT: this must be able to lock the context.
static void state_set(context_t *context, fgr_state_t state)
{
    if (context->lock) {

        CONTEXT_LOCK(context->lock, "state_set()");
        context->state = state;
        CONTEXT_UNLOCK(context->lock, "state_set()");
    }
}

// Get the node's state.
// IMPORTANT: this must be able to lock the context.
static int32_t state_get(context_t *context)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (context->lock) {

        CONTEXT_LOCK(context->lock, "state_get()");
        err = (int32_t) context->state;
        CONTEXT_UNLOCK(context->lock, "state_get()");
    }

    return err;
}

// Set our configuration based on a message contents,
// which should be a single, big-endian, uint32_t.
static int32_t msg_cfg_set(context_t *context,
                           const uint8_t *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint32_t uint32_network_byte_order;

    if (context && buffer && (length >= sizeof(uint32_network_byte_order))) {
        CONTEXT_LOCK(context->lock, "msg_cfg_set()");
        // Must do a copy as buffer may not be 4-byte aligned
        memcpy(&uint32_network_byte_order, &(buffer[0]), sizeof(uint32_network_byte_order));
        context->reporting_interval_seconds = ntohl(uint32_network_byte_order);
        CONTEXT_UNLOCK(context->lock, "msg_cfg_set()");
        err = ESP_OK;
    }

    return err;
}

// Get our configuration into a message contents;
// a single, big-endian, uint32_t.
static int32_t msg_cfg_get(context_t *context,
                           uint8_t *buffer, size_t length)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint32_t uint32_network_byte_order;

    if (context && buffer && (length >= sizeof(uint32_network_byte_order))) {
        CONTEXT_LOCK(context->lock, "msg_cfg_get()");
        // Must do a copy as buffer may not be 4-byte aligned
        uint32_network_byte_order = htonl(context->reporting_interval_seconds);
        memcpy(&(buffer[0]), &uint32_network_byte_order, sizeof(uint32_network_byte_order));
        err = sizeof(uint32_network_byte_order);
        CONTEXT_UNLOCK(context->lock, "msg_cfg_get()");
    }

    return err;
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLBACKS
 * -------------------------------------------------------------- */

// Callback to obtain the state of this node.
static fgr_state_t state_cb(void *param)
{
    context_t *context = (context_t *) param;
    fgr_state_t state = FGR_STATE_NOT_POPULATED;

    if (context->lock) {

        CONTEXT_LOCK(context->lock, "state_cb()");
        state = context->state;
        CONTEXT_UNLOCK(context->lock, "state_cb()");
    }

    return state;
}

// Message receive callback.
static bool msg_receive_cb(fgr_msg_t *msg, void *param)
{
    context_t *context = (context_t *) param;
    bool handled = false;
    uint32_t length = 0;
    uint8_t contents[FGR_MSG_CONTENTS_MAX_LEN];

    fgr_error_t msg_error = FGR_ERROR_UNHANDLED_REQUEST;

    if (IS_MSG_REQ(msg->header.req.type)) {
        // REQUEST messages
        handled = true;
        switch (MSG_MASK(msg->header.req.type)) {
            case FGR_REQ_CNF_CFG:
                msg_error = FGR_ERROR_INVALID_PARAM;
                if (msg_cfg_set(context, msg->body.contents,
                                msg->body.length) == ESP_OK) {
                    msg_error = FGR_ERROR_NONE;
                    ESP_LOGI(TAG, "Reporting interval set to %d second(s).",
                             state_get(context));
                } else {
                    ESP_LOGE(TAG, "Unable to set reporting interval,"
                             " it will remain %d second(s).", state_get(context));
                }
                // We're somewhat in limbo if the above fails,
                // best to start anyway with the default interval;
                // we will return the current configuration in
                // the confirmation anyway
                state_set(context, FGR_STATE_STARTED);
                msg_cfg_get(context, contents, sizeof(contents));
            break;
            case FGR_REQ_CNF_START:
                state_set(context, FGR_STATE_STARTED);
                msg_error = FGR_ERROR_NONE;
            break;
            case FGR_REQ_CNF_STOP:
                state_set(context, FGR_STATE_STOPPED);
                msg_error = FGR_ERROR_NONE;
            break;
            case FGR_REQ_CNF_REBOOT:
                // Just reset the running flag and we will exit
                context->running = false;
                state_set(context, FGR_STATE_STOPPED);
                msg_error = FGR_ERROR_NONE;
            break;
            default:
                handled = false;
            break;
        }

        if (handled) {
            fgr_msg_send_queue_cnf(MSG_MASK(msg->header.req.type), msg_error,
                                   msg->header.req.reference,
                                   contents, length);
        }
    } else {
        // RESPONSE messages
        handled = true;
        switch (MSG_MASK(msg->header.req.type)) {
            case FGR_IND_RSP_NEEDS_CFG:
                msg_error = FGR_ERROR_INVALID_PARAM;
                if (msg_cfg_set(context, msg->body.contents,
                                msg->body.length) == ESP_OK) {
                    msg_error = FGR_ERROR_NONE;
                    ESP_LOGI(TAG, "Reporting interval set to %d second(s).",
                             state_get(context));
                } else {
                    ESP_LOGE(TAG, "Unable to set reporting interval,"
                             " it will remain %d second(s).", state_get(context));
                }
                // We're somewhat in limbo if the above fails,
                // best to start anyway with the default interval
                // and return the current configuration in the
                // FGR_IND_RSP_START
                state_set(context, FGR_STATE_STARTED);
                msg_cfg_get(context, contents, sizeof(contents));
                fgr_msg_send_queue_ind(FGR_IND_RSP_START, contents, length);
            break;
            case FGR_IND_RSP_START:
            case FGR_IND_RSP_STOP:
                // Ignore
            break;
            default:
                handled = false;
            break;
        }
    }

    if (handled) {
        // This will be printed before the queued messages are sent
        fgr_msg_print_summary("Handled", FGR_LOG_LEVEL_INFO, msg->header.req.type, 0,
                              msg->header.req.reference, msg->body.length);
    }

    return handled;
}

// Callback for message sends.
static void send_cb(void *param)
{
    (void) param;

    // Indicate that we are alive
    fgr_debug_flash_led(FGR_DEBUG_LED_SHORT_MS, FGR_DEBUG_LED_COLOUR_NOTIFY);
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: INITIALISATION/DEINITIALISATION
 * -------------------------------------------------------------- */

// Generic initialisation.
static int32_t init(context_t *context)
{
    // Print out our Wi-Fi MAC address
    fgr_debug_print_mac_address();

    // Create the default event loop, for everyone's use
    int32_t err = esp_event_loop_create_default();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create default event loop: %s.", esp_err_to_name(err));
    }

    // Configure our debug LED
    if (err == ESP_OK) {
        err = fgr_debug_init();
    }

    // Create mutex for the application's context
    if (err == ESP_OK) {
        err = -ESP_ERR_NO_MEM;
        context->lock = xSemaphoreCreateMutex();
        if (context->lock) {
            err = ESP_OK;
        }
    }

#if !defined(CONFIG_FGR_APP_NO_WIFI)
    // Initialise OTA
    if (err == ESP_OK) {
        err = fgr_ota_init();
    }

    // Initialize networking
    if (err == ESP_OK) {
        err = fgr_network_init(CONFIG_FGR_NETWORK_WIFI_SSID,
                               CONFIG_FGR_NETWORK_WIFI_PASSWORD,
                               WIFI_AUTH_OPEN,
                               CONFIG_FGR_NETWORK_WIFI_REDUCED_TX_POWER);
    }

    // Check for an OTA update, which may restart the system
    if (err == ESP_OK) {
        err = fgr_ota_update(CONFIG_FGR_OTA_FIRMWARE_UPGRADE_URL,
                             (const char *) g_server_cert_pem_start,
                             CONFIG_FGR_OTA_RECEIVE_TIMEOUT_MS);
    }

    // Forward logging to the server
    if (err == ESP_OK) {
        err = fgr_log_init(CONFIG_FGR_NETWORK_CONTROLLER_IP_ADDRESS,
                           CONFIG_FGR_LOG_PORT, FGR_LOG_LEVEL_INFO);
    }

    // Initialise messaging
    if (err == ESP_OK) {
        err = fgr_msg_init(CONFIG_FGR_NETWORK_CONTROLLER_IP_ADDRESS,
                           CONFIG_FGR_MSG_PORT,
                           CONFIG_FGR_MSG_HEARTBEAT_SECONDS,
                           state_cb, context);
    }

    // For debug purposes, hook-in a message send callback
    if (err == ESP_OK) {
        err = fgr_msg_send_cb(send_cb, context);
    }

    // Create a message send queue
    if (err == ESP_OK) {
        err = fgr_msg_send_queue_init(FGR_MSG_SEND_QUEUE_LENGTH);
    }

#else
    ESP_LOGW(TAG, "CONFIG_FGR_APP_NO_WIFI is defined, not connecting to WiFi.");
#endif

    // Node-specific initialisation
    if (err == ESP_OK) {
        // Configure the RCWL-9610A driver
        err = fgr_rcwl9610a_init(CONFIG_FGR_RCWL9610A_UART_NUM,
                                 CONFIG_FGR_RCWL9610A_UART_TXD_PIN,
                                 CONFIG_FGR_RCWL9610A_UART_RXD_PIN);
    }

    return err;
}

// Shutdown.
static void deinit(context_t *context)
{
    fgr_rcwl9610a_deinit();
    fgr_msg_deinit();
    fgr_log_deinit();
    fgr_network_deinit();
    fgr_debug_deinit();
    vSemaphoreDelete(context->lock);
    esp_restart();
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: NODE SPECIFIC
 * -------------------------------------------------------------- */

// What this node does.
static void do_node(context_t *context)
{
    // Allow us to feed the watchdog
    esp_task_wdt_add(NULL);

    // Main loop
    while(context->running) {
        if (state_cb(context) == FGR_STATE_STARTED) {
            int32_t err = fgr_rcwl9610a_read();
            if (err >= 0) {
                ESP_LOGI(TAG, "Distance %d mm.", err);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_task_wdt_reset();
    }

    esp_task_wdt_delete(NULL);
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Entry point.
void app_main(void)
{
    context_t context = {
        .state = FGR_STATE_NOT_POPULATED,
        .running = true,
        .reporting_interval_seconds = REPORTING_INTERVAL_DEFAULT_SECONDS
    };

    ESP_LOGI(TAG, "app_main start, default reporting interval"
             " %d second(s).", state_get(&context));

    int32_t err = init(&context);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Initialization complete.");
        // Start receiving messages
        err = fgr_msg_receive_start();
    }
    if (err == ESP_OK) {
        // Add the logging received message handler
        err = fgr_msg_receive_handler_add(0, fgr_log_msg_receive_cb, NULL);
    }
    if (err == ESP_OK) {
        // Add our received message handler
        err = fgr_msg_receive_handler_add(0, msg_receive_cb, &context);
    }

    if (err == ESP_OK) {
        // Indicate that we need configuration (measurement reporting interval)
        state_set(&context, FGR_STATE_NEEDS_CFG);
        err = fgr_msg_send_ind(FGR_IND_RSP_NEEDS_CFG, NULL, 0);
    }

    if (err == ESP_OK) {
        // Finally, do the stuff of this node
        do_node(&context);
    } else {
        // Only get here if there has been a problem
        state_set(&context, FGR_STATE_GENERIC_FAILED);
        ESP_LOGE(TAG, "Setup failed (%s), will restart soonish.", esp_err_to_name(-err));
    }

    // Wait a while to let any messages leave the building
    vTaskDelay(pdMS_TO_TICKS(2000));

    deinit(&context);
}

// End of file
