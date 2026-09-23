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
 * @brief Application to drive the Big Round Button, a node on the
 * front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_event.h"
#include "esp_log.h"
#include "errno.h"
#include "esp_task_wdt.h"
#include "driver/gpio.h"
#include "esp_timer.h"

#include "../../../../protocol/fgr_protocol.h"
#include "fgr_util.h"
#include "fgr_monitor.h"
#include "fgr_msg.h"
#include "fgr_debug.h"
#include "fgr_metrics.h"
#include "fgr_log.h"
#include "fgr_lib.h"

// Must be last in the inclusions to poison calls to malloc()/free()
#include "fgr_heap_wrapper.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix.
#define TAG "brb"

// The message heartbeat period.
#ifndef FGR_MSG_HEARTBEAT_SECONDS
#  define FGR_MSG_HEARTBEAT_SECONDS 25
#endif

#ifdef CONFIG_FGR_APP_LED_WS2812_GRB
#  define FGR_APP_LED_WS2812_GRB 1
#else
#  define FGR_APP_LED_WS2812_GRB 0
#endif

// Debounce timer in milliseconds.
#ifndef DEBOUNCE_POLL_INTERVAL_MS
#  define DEBOUNCE_POLL_INTERVAL_MS 10
#endif

// Debounce threshold.
#ifndef DEBOUNCE_THRESHOLD
#  define DEBOUNCE_THRESHOLD 3
#endif

// How long an activation lasts in seconds.
#ifndef ACTIVATED_SECONDS
#  define ACTIVATED_SECONDS 10
#endif

// The "ready" colour.
#ifndef COLOUR_READY
#  define COLOUR_READY FGR_WS2812_LED_COLOUR_GREEN
#endif

// The "active" colour.
#ifndef COLOUR_ACTIVE
#  define COLOUR_ACTIVE FGR_WS2812_LED_COLOUR_BLUE
#endif

// The "bad" colour.
#ifndef COLOUR_BAD
#  define COLOUR_BAD FGR_WS2812_LED_COLOUR_RED
#endif

// Short flash duration in milliseconds.
#ifndef FLASH_DURATION_SHORT_MS
#  define FLASH_DURATION_SHORT_MS 250
#endif

// Long flash duration in milliseconds.
#ifndef FLASH_DURATION_LONG_MS
#  define FLASH_DURATION_LONG_MS 1000
#endif

// Very long flash duration in milliseconds.
#ifndef FLASH_DURATION_VERY_LONG_MS
#  define FLASH_DURATION_VERY_LONG_MS 4000
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// The state of the button.
typedef enum {
    BUTTON_STATE_NULL,
    BUTTON_STATE_READY,
    BUTTON_STATE_TRIGGERED,
    BUTTON_STATE_ACTIVE
} button_state_t;

// Context.
typedef struct {
    fgr_state_t state;
    SemaphoreHandle_t lock;
    bool running;
    QueueHandle_t queue_handle;
    void *led_handle;
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
    uint8_t *contents = (uint8_t *) MALLOC(FGR_MSG_CONTENTS_MAX_LEN);

    fgr_error_t msg_error = FGR_ERROR_UNHANDLED_REQUEST;

    if (contents) {
        if (IS_MSG_REQ(msg->header.req.type)) {
            // REQUEST messages
            handled = true;
            switch (MSG_MASK(msg->header.req.type)) {
                case FGR_REQ_CNF_CFG:
                    // No configuration, just confirm
                    state_set(context, FGR_STATE_STARTED);
                    msg_error = FGR_ERROR_NONE;
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
                    // No configuration required, nothing to do,
                    // just set state to started and indicate
                    // that we have
                    state_set(context, FGR_STATE_STARTED);
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

        FREE(contents);
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
    fgr_debug_led_flash(FGR_DEBUG_LED_SHORT_MS, FGR_DEBUG_LED_COLOUR_MSG_SENT);
}

// Callback for when a restart is about to happen.
static void restart_cb(void *param)
{
    (void) param;
    // Nothing to do.
}

// Callback to confirm to the OTA code that all is good.
static bool is_good_cb(void *param)
{
    (void) param;
    // Nothing to check.
    return true;
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

    // Create mutex for the application's context
    if (err == ESP_OK) {
        err = -ESP_ERR_NO_MEM;
        context->lock = xSemaphoreCreateMutex();
        if (context->lock) {
            err = ESP_OK;
        }
    }

    // Initialise all of the libraries
    if (err == ESP_OK) {
        err = fgr_lib_init((const char *) g_server_cert_pem_start,
                           state_cb, send_cb, restart_cb, is_good_cb,
                           context);
    }

    // Node-specific initialisation follows

    // Initialise the chain of WS2812 LEDs
    if (err == ESP_OK) {
        err = fgr_ws2812_chain_init(CONFIG_FGR_APP_LED_SPI_NUM, -1,
                                    CONFIG_FGR_APP_LED_PIN,
                                    CONFIG_FGR_APP_LED_COUNT,
                                    FGR_APP_LED_WS2812_GRB,
                                    &context->led_handle);
        if (err == ESP_OK) {
            // Flash all of the LEDs white so that we know they are on
            err = fgr_ws2812_led_flash(context->led_handle, -1,
                                       FLASH_DURATION_LONG_MS,
                                       FGR_WS2812_LED_COLOUR_WHITE);
        }
    }

    // Configure the GPIO for the microswitch
    if (err == ESP_OK) {
        gpio_config_t cfg = {
            .mode = GPIO_MODE_INPUT,
            .pin_bit_mask = (1UL << CONFIG_FGR_APP_SWITCH_PIN),
            // Enable pull-up (the switch when active will ground the pin)
            .pull_up_en = GPIO_PULLUP_ENABLE
        };
        err = -gpio_config(&cfg);
    }

    return err;
}

// Shutdown.
static void deinit(context_t *context)
{
    // Switch the LEDs off and deinitialise the chain
    fgr_ws2812_led_set(context->led_handle, -1, FGR_WS2812_LED_COLOUR_NONE);
    vTaskDelay(pdMS_TO_TICKS(100));
    fgr_ws2812_chain_deinit(context->led_handle);
    context->led_handle = NULL;

    // Generic deinitialisation follows

    fgr_lib_deinit();
    vSemaphoreDelete(context->lock);
    esp_restart();
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: NODE SPECIFIC
 * -------------------------------------------------------------- */

// What this node does.
static void do_node(context_t *context)
{
    button_state_t state = BUTTON_STATE_NULL;
    bool switch_pressed = false;
    bool switch_toggled = false;
    size_t stable_count = 0;
    int64_t activated_us = 0;

    // Set all of the LEDs to the "ready" colour and breathing
    int32_t err = fgr_ws2812_led_set(context->led_handle, -1, COLOUR_READY);
    if (err == ESP_OK) {
        err = fgr_ws2812_led_breathe_on(context->led_handle, -1);
    } else {
        ESP_LOGE(TAG, "fgr_ws2812_led_set() returned %s!", esp_err_to_name(-err));
    }
    if (err == ESP_OK) {
        state = BUTTON_STATE_READY;
    } else {
        ESP_LOGE(TAG, "fgr_ws2812_led_breathe_on() returned %s!", esp_err_to_name(-err));
    }

    // Note: don't check for errors after this, if the above survived
    // we're likely all good

    ESP_LOGI(TAG, "starting, in READY state.");

    // Get the initial switch reading and wait for at least the debounce period
    uint8_t last_raw = gpio_get_level(CONFIG_FGR_APP_SWITCH_PIN);
    vTaskDelay(pdMS_TO_TICKS(DEBOUNCE_POLL_INTERVAL_MS));

    // Main loop
    while (context->running && (err == ESP_OK)) {

        // Read and debounce switch
        uint8_t raw = gpio_get_level(CONFIG_FGR_APP_SWITCH_PIN);
        if (raw == last_raw) {
            if (stable_count < DEBOUNCE_THRESHOLD) {
                stable_count++;
            }
        } else {
            last_raw = raw;
            stable_count = 0;
        }

        // Check if switch is pressed after debouncing
        if (stable_count >= DEBOUNCE_THRESHOLD) {
            switch_toggled = (switch_pressed != (raw == 0));
            switch_pressed = (raw == 0);
            if (switch_toggled) {
                if (switch_pressed) {
                    ESP_LOGI(TAG, "button pressed.");
                    // If the switch is pressed and has toggled while we are in
                    // "active" state, flash to indicate "bad"
                    if (state == BUTTON_STATE_ACTIVE) {
                        ESP_LOGI(TAG, "button pressed while active.");
                        fgr_ws2812_led_flash(context->led_handle, -1,
                                            FLASH_DURATION_SHORT_MS,
                                            COLOUR_BAD);
                    } else {
                        ESP_LOGI(TAG, "state TRIGGERED.");
                        state = BUTTON_STATE_TRIGGERED;
                    }
                } else {
                    ESP_LOGI(TAG, "button released.");
                }
            }
        }

        // Switch to active state if we've been triggered
        if (state == BUTTON_STATE_TRIGGERED) {
            ESP_LOGI(TAG, "state ACTIVE.");
            state = BUTTON_STATE_ACTIVE;
            activated_us = esp_timer_get_time();

            // Change the LED colour to the "active" colour
            fgr_ws2812_led_set(context->led_handle, -1, COLOUR_ACTIVE);

            // Since the colour of a breathing LED won't change mid-breath
            // flash all of the LEDs in the active colour to confirm
            // the button press
            fgr_ws2812_led_flash(context->led_handle, -1,
                                       FLASH_DURATION_VERY_LONG_MS,
                                       COLOUR_ACTIVE);
        }

        // Switch active state off after some time
        if ((state == BUTTON_STATE_ACTIVE) && (esp_timer_get_time() - activated_us > ACTIVATED_SECONDS * 1000000)) {
            // Activation is over, change back to the "ready" colour
            ESP_LOGI(TAG, "state READY.");
            state = BUTTON_STATE_READY;
            fgr_ws2812_led_set(context->led_handle, -1, COLOUR_READY);
        }

        vTaskDelay(pdMS_TO_TICKS(DEBOUNCE_POLL_INTERVAL_MS));
        esp_task_wdt_reset();
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Entry point.
void app_main(void)
{
    context_t context = {
        .state = FGR_STATE_NOT_POPULATED,
        .running = true
    };

    ESP_LOGI(TAG, "app_main start.");

    int32_t err = init(&context);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Initialization complete.");
        // Start receiving messages
        err = fgr_msg_receive_start();
    }
    if (err == ESP_OK) {
        // Add our received message handler
        err = fgr_msg_receive_handler_add(0, msg_receive_cb, &context);
    }

    if (err == ESP_OK) {
        // Indicate that we need configuration
        state_set(&context, FGR_STATE_NEEDS_CFG);
        err = fgr_msg_send_ind(FGR_IND_RSP_NEEDS_CFG, NULL, 0);
    }

    if (err == ESP_OK) {
        // Finally, do the stuff of this node
        do_node(&context);
    } else {
        // Only get here if there has been a problem
        state_set(&context, FGR_STATE_GENERIC_FAILED);
        fgr_metrics_event_set(FGR_METRIC_EVENT_LOCAL_REBOOT, -err);
        ESP_LOGE(TAG, "Setup failed (%s), will restart soonish.", esp_err_to_name(-err));
    }

    // Wait a while to let any messages leave the building
    vTaskDelay(pdMS_TO_TICKS(1000));

    deinit(&context);
}

// End of file
