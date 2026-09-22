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
 * @brief Debug utilities for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_log.h"
#include "errno.h"
#include "ctype.h"
#include "esp_mac.h"
#include "driver/gpio.h"
#include "esp_core_dump.h"
#include "esp_partition.h"
#include "esp_app_desc.h"
#include "mbedtls/base64.h"

#include "fgr_util.h"
#include "fgr_ws2812.h"
#include "fgr_monitor.h"
#include "fgr_task.h"
#include "fgr_nvs.h"
#include "fgr_rram.h"
#include "fgr_msg.h"

#include "fgr_debug.h"

// Forward declaration of the abstracted panic info structure from ESP-IDF
void __real_esp_panic_handler(void *info);
// Make sure the linker doesn't optimize-out our wrapper
void __wrap_esp_panic_handler(void *info) __attribute__((used));

// Must be last in the inclusions to poison calls to malloc()/free()
#include "fgr_heap_wrapper.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "debug"

#ifndef NVS_NAME_LED_MASKED
// A name for the field that masks the LED off in NV storage.
#  define NVS_NAME_LED_MASKED "led_masked"
#endif

#ifndef NVS_NAME_LED_BREATHE_ENABLED
// A name for the field that enables LED "breathing" in NV storage.
// Note: not "led_breathe_enabled" as that turns out to be too long.
#  define NVS_NAME_LED_BREATHE_ENABLED "led_breathe_on"
#endif

#ifndef CORE_DUMP_BASE64_CHUNK_LENGTH
// The maximum length of chunk of a core dump to base64 encode; the
// raw chunk length before base64 encoding will be 3/4 of this size.
#  define CORE_DUMP_BASE64_CHUNK_LENGTH 512
#endif

// The amount of core dump raw data that can be base64 encoded
// into CORE_DUMP_BASE64_CHUNK_LENGTH.
#define CORE_DUMP_CHUNK_LENGTH (CORE_DUMP_BASE64_CHUNK_LENGTH * 3 / 4)

// The base64 chunk length must be a multiple of four to
// accommodate base64 cleanly.
#if CORE_DUMP_BASE64_CHUNK_LENGTH % 4 != 0
#  error CORE_DUMP_CHUNK_LENGTH must be such that CORE_DUMP_BASE64_CHUNK_LENGTH is a multiple of four
#endif

// mbedTLS requires room for a null terminator in the buffer
#define CORE_DUMP_BASE64_CHUNK_LENGTH_MBEDTLS (CORE_DUMP_BASE64_CHUNK_LENGTH + 1)

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Context.
typedef struct {
    SemaphoreHandle_t lock;
    bool initialised;
    fgr_debug_state_cb_t cb;
    void *cb_param;
    void *ws2812_handle;
    bool led_masked_off;
    bool breathe_enabled;
} context_t;

// Storage for a backtrace.
typedef struct {
    uint8_t hash[32];
    uint8_t length;
    uint32_t address_list[FGR_DEBUG_BACKTRACE_DEPTH_MAX];
} backtrace_t;

// Storage for an overflowing task name.
// Note: name _must_ be at the start of the structure,
// see vApplicationStackOverflowHook() for why.
typedef struct {
    char name[FGR_UTIL_TASK_NAME_MAX_LENGTH];
} stack_overflow_task_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Context.
static context_t g_context = {0};

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

// Table of states to breathe colours
static const fgr_ws2812_colour_t g_state_to_breathe_colour[] = {FGR_DEBUG_LED_COLOUR_BOOT,      // FGR_STATE_NOT_POPULATED (0)
                                                                FGR_DEBUG_LED_COLOUR_NEEDS_CFG, // FGR_STATE_NEEDS_CFG (1)
                                                                FGR_DEBUG_LED_COLOUR_GOOD,      // FGR_STATE_STARTED (2)
                                                                FGR_DEBUG_LED_COLOUR_STOPPED,   // FGR_STATE_STOPPED (3)
                                                                FGR_DEBUG_LED_COLOUR_BAD,       // FGR_STATE_DISCONNECTED (4)
                                                                FGR_DEBUG_LED_COLOUR_BAD,       // FGR_STATE_GENERIC_FAILED (5)
                                                                FGR_DEBUG_LED_COLOUR_BAD};      // FGR_STATE_HARDWARE_FAILURE (6)

#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

// The names of the ESP32 reset reasons, in the order of
// esp_reset_reason_t, for easy display.
static const char *g_esp_reset_reason[]  = {"UNKNOWN",      // ESP_RST_UNKNOWN
                                            "POWERON",      // ESP_RST_POWERON
                                            "EXT",          // ESP_RST_EXT
                                            "SW",           // ESP_RST_SW
                                            "PANIC",        // ESP_RST_PANIC
                                            "INT_WDT",      // ESP_RST_INT_WDT
                                            "TASK_WDT",     // ESP_RST_TASK_WDT
                                            "WDT",          // ESP_RST_WDT
                                            "DEEPSLEEP",    // ESP_RST_DEEPSLEEP
                                            "BROWNOUT",     // ESP_RST_BROWNOUT
                                            "SDIO",         // ESP_RST_SDIO
                                            "USB",          // ESP_RST_USB
                                            "JTAG",         // ESP_RST_JTAG
                                            "EFUSE",        // ESP_RST_EFUSE
                                            "PWR_GLITCH",   // ESP_RST_PWR_GLITCH
                                            "CPU_LOCKUP"};  // ESP_RST_CPU_LOCKUP

// Storage for backtrace address list in retained RAM.
FGR_RRAM_DEFINE(backtrace_t, backtrace);

// Storage for an overflowing stack's name in retained RAM.
FGR_RRAM_DEFINE(stack_overflow_task_t, stack_overflow_task);

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: NVS RELATED
 * -------------------------------------------------------------- */

// Retrieve whether the LED is masked off or not from NVS.
static int32_t nvs_led_masked_get(bool *masked)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint32_t value = 0;

    if (masked) {
        err = fgr_nvs_get(NVS_NAME_LED_MASKED, &value);
        if (err == ESP_OK) {
            *masked = (value != 0);
        }
    }

    return err;
}

// Set whether LED is masked off or not in NVS.
static int32_t nvs_led_masked_set(bool masked)
{
    return fgr_nvs_set(NVS_NAME_LED_MASKED, masked);
}

// Retrieve whether LED "breathing" is enabled from NVS.
static int32_t nvs_led_breathe_enabled_get(bool *enabled)
{
    int32_t err = -ESP_ERR_INVALID_ARG;
    uint32_t value = 0;

    if (enabled) {
        err = fgr_nvs_get(NVS_NAME_LED_BREATHE_ENABLED, &value);
        if (err == ESP_OK) {
            *enabled = (value != 0);
        }
    }

    return err;
}

// Set whether LED "breathing" is enabled in NVS.
static int32_t nvs_led_breathe_enabled_set(bool enabled)
{
    return fgr_nvs_set(NVS_NAME_LED_BREATHE_ENABLED, enabled);
}

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: CALLBACKS
 * -------------------------------------------------------------- */

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

// Convert an fgr_state_t into the corresponding set of fgr_ws2812_colour_t.
static fgr_ws2812_colour_t fgr_state_to_colour(fgr_state_t state)
{
    // This should come out as a dull orange if the mapping fails
    fgr_ws2812_colour_t colour = {200, 120, 0};

    if (state < FGR_UTIL_ARRAY_LENGTH(g_state_to_breathe_colour)) {
        colour = g_state_to_breathe_colour[state];
    }

    return colour;
}

// Callback to be passed to fgr_ws2812_led_set_cb() to set the debug LED colour.
static void ws2812_colour_cb(fgr_ws2812_colour_t *colour, void *param)
{
    context_t *context = (context_t *) param;

    if (colour && context && context->cb) {
        *colour = fgr_state_to_colour(context->cb(context->cb_param));
    }
}

#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Set whether the LED is masked off or not.
static void led_masked_off(context_t *context, bool masked)
{
    if (context->lock) {

        CONTEXT_LOCK(context->lock, "led_masked_off()");
        context->led_masked_off = masked;
        nvs_led_masked_set(masked);
        if (masked) {
            fgr_ws2812_led_mask_off(context->ws2812_handle, 0);
        } else {
            fgr_ws2812_led_mask_on(context->ws2812_handle, 0);
        }
        CONTEXT_UNLOCK(context->lock, "led_masked_off()");
    }
}

// Set whether LED "breathing" is enabled.
static void led_breathe_enabled(context_t *context, bool enabled)
{
    if (context->lock) {

        CONTEXT_LOCK(context->lock, "led_breathe_enabled()");

        context->breathe_enabled = enabled;
        nvs_led_breathe_enabled_set(enabled);
        if (enabled) {
            // Resume breathing according to the callback
            fgr_ws2812_led_set_cb(g_context.ws2812_handle, 0,
                                    ws2812_colour_cb, &g_context);
        } else {
            // The desired effect is that the LED is off but flashes
            // can still occur.  The static colour will be none in any
            // case (because this code never sets it), so all we need
            // to do is not set the LED colour based on the callback
            fgr_ws2812_led_set(context->ws2812_handle, 0, FGR_DEBUG_LED_COLOUR_NONE);
            fgr_ws2812_led_set_cb(g_context.ws2812_handle, 0,
                                    NULL, NULL);
        }

        CONTEXT_UNLOCK(context->lock, "led_breathe_enabled()");
    }
}

// Log a debug string, used by the panic, stack overflow
// and core dump reporting functions.
static void debug_log(const char *tag, const char *prefix,
                      const char *string, esp_log_level_t level)
{
    if (string && (level > ESP_LOG_NONE)) {
        if (!tag) {
            tag = TAG;
        }
        if (!prefix) {
            prefix = "";
        }
        switch (level) {
            case ESP_LOG_ERROR:
                ESP_LOGE(tag, "%s%s", prefix, string);
                break;
            case ESP_LOG_WARN:
                ESP_LOGW(tag, "%s%s", prefix, string);
                break;
            case ESP_LOG_INFO:
                ESP_LOGI(tag, "%s%s", prefix, string);
                break;
            case ESP_LOG_DEBUG:
                ESP_LOGD(tag, "%s%s", prefix, string);
                break;
            case ESP_LOG_VERBOSE:
                ESP_LOGD(tag, "%s%s", prefix, string);
            default:
                break;
        }
    }
}

// Format a 32-byte hash into a null-terminated string.
static void hash_str(char *buffer_str, const uint8_t *hash)
{
    if (buffer_str && hash) {
        // Convert the raw 32-byte binary SHA256 into a readable hex string
        for (size_t x = 0; x < FGR_DEBUG_HASH_LENGTH; x++) {
            buffer_str += sprintf(buffer_str, "%02x", *(hash + x));
        }
        *buffer_str = '\0'; // null-terminate
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: INITIALISE/DEINITIALISE
 * -------------------------------------------------------------- */

// Initialise debug stuff.
int32_t fgr_debug_init(fgr_debug_state_cb_t cb, void *cb_param)
{
    int32_t err = -ESP_ERR_NO_MEM;

    if (!g_context.lock) {
        g_context.lock = xSemaphoreCreateMutex();
    }

    if (g_context.lock) {
        err = ESP_OK;
        if (!g_context.initialised) {

            CONTEXT_LOCK(g_context.lock, "fgr_debug_init()");

            g_context.led_masked_off = false;
            g_context.breathe_enabled = true;

            // Read values from non-volatile storage and,
            // if not present, write the default value back
            if (nvs_led_masked_get(&g_context.led_masked_off) != ESP_OK) {
                nvs_led_masked_set(g_context.led_masked_off);
            }
            if (nvs_led_breathe_enabled_get(&g_context.breathe_enabled) != ESP_OK) {
                nvs_led_breathe_enabled_set(g_context.breathe_enabled);
            }

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally
            // Create a WS2812 LED chain for the debug LED
            err = fgr_ws2812_chain_init(CONFIG_FGR_DEBUG_LED_SPI_NUM, -1,
                                        CONFIG_FGR_DEBUG_LED_PIN, 1,
                                        CONFIG_FGR_DEBUG_LED_WS2812_GRB,
                                        &g_context.ws2812_handle);
            if ((err == ESP_OK) && cb) {
                g_context.cb = cb;
                g_context.cb_param = cb_param;
                //  Set the single debug LED according to the callback
                err = fgr_ws2812_led_set_cb(g_context.ws2812_handle, 0,
                                            ws2812_colour_cb, &g_context);
                if ((err == ESP_OK) && g_context.breathe_enabled) {
                    err = fgr_ws2812_led_breathe_on(g_context.ws2812_handle, 0);
                }
            }
#  else
            // Configure our single colour debug LED
            err = gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);
            if (err == ESP_OK) {
                err = gpio_set_direction(CONFIG_FGR_DEBUG_LED_PIN, GPIO_MODE_OUTPUT);
                if (err == ESP_OK) {
                    ESP_LOGI(TAG, "Using a single colour debug LED on pin %d.",
                             CONFIG_FGR_DEBUG_LED_PIN);
                } else {
                    ESP_LOGE(TAG, "gpio_set_direction() on pin %d failed (%s)!",
                             CONFIG_FGR_DEBUG_LED_PIN, esp_err_to_name(err));
                }
            } else {
                ESP_LOGE(TAG, "gpio_set_level() on pin %d failed (%s)!",
                         CONFIG_FGR_DEBUG_LED_PIN, esp_err_to_name(err));
            }
            // Return ESP_OK or negative error code from esp_err_t
            err = -err;
#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

            if (err == ESP_OK) {
                g_context.initialised = true;
            }

            CONTEXT_UNLOCK(g_context.lock, "fgr_debug_init()");

            if (err == ESP_OK) {
                // Flash the LED so that we know it can be active
                fgr_debug_led_flash(FGR_DEBUG_LED_LONG_MS, FGR_DEBUG_LED_COLOUR_BOOT);
            }
        }
    }

    return err;
}

// Deinitialise debug stuff.
void fgr_debug_deinit()
{
    if (g_context.lock) {

        ESP_LOGI(TAG, "Stopping debug.");

        CONTEXT_LOCK(g_context.lock, "fgr_debug_deinit()");

        if (g_context.ws2812_handle) {
            fgr_ws2812_led_set(g_context.ws2812_handle, 0, FGR_DEBUG_LED_COLOUR_NONE);
            vTaskDelay(pdMS_TO_TICKS(100));
            fgr_ws2812_chain_deinit(g_context.ws2812_handle);
            g_context.ws2812_handle = NULL;
        }

        // Forget any callback
        g_context.cb = NULL;
        g_context.cb_param = NULL;

        g_context.initialised = false;

        CONTEXT_UNLOCK(g_context.lock, "fgr_debug_deinit()");
        // The semaphore will be re-used
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: LED RELATED
 * -------------------------------------------------------------- */

// Flash the debug LED.
void fgr_debug_led_flash(int32_t duration_ms, fgr_ws2812_colour_t colour)
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_debug_led_flash()");

        if (!g_context.led_masked_off) {
            if (g_context.ws2812_handle) {
                // WS2812 LED
                fgr_ws2812_led_flash(g_context.ws2812_handle, 0,
                                     duration_ms, colour);
            } else {
                // Single colour LED
                gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 0);
                vTaskDelay(pdMS_TO_TICKS(duration_ms));
                gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);
            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_debug_led_flash()");
    }
}

// Set the LED "breathing" a specific colour.
void fgr_debug_led_breathe_set(fgr_ws2812_colour_t colour)
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_debug_set_breathe()");

        if (!g_context.led_masked_off && g_context.breathe_enabled) {
            if ((colour.red == 0) && (colour.green == 0) && (colour.blue == 0)) {
                // User wants to switch automatic breathing according to the
                // callback on again
                fgr_ws2812_led_set_cb(g_context.ws2812_handle, 0,
                                      ws2812_colour_cb, &g_context);
            } else {
                // User wants to set a "manual" breathing colour
                if (fgr_ws2812_led_set(g_context.ws2812_handle, 0, colour) == ESP_OK) {
                    // The callback mechanism overrides manually set breathing,
                    // so need to switch it off
                    fgr_ws2812_led_set_cb(g_context.ws2812_handle, 0, NULL, NULL);
                }
            }
        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_debug_set_breathe()");
    }
}

// Turn the LED "breathe" effect off.
void fgr_debug_led_breathe_off(void)
{
    led_breathe_enabled(&g_context, false);
}

// Turn the LED "breathe" effect on.
void fgr_debug_led_breathe_on(void)
{
    led_breathe_enabled(&g_context, true);
}

// Turn all debug LEDs off.
void fgr_debug_led_off(void)
{
    led_masked_off(&g_context, true);
}

// Allow all debug LEDs to operate.
void fgr_debug_led_on(void)
{
    led_masked_off(&g_context, false);
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: PANIC
 * -------------------------------------------------------------- */

// Wrapper function to save backtrace on panic.
void IRAM_ATTR __wrap_esp_panic_handler(void *param)
{
    uint32_t pc;
    uint32_t sp;
    uint32_t next_pc;
    esp_backtrace_frame_t frame;

    esp_backtrace_get_start(&pc, &sp, &next_pc);
    frame.pc = pc;
    frame.sp = sp;
    frame.next_pc = next_pc;
    frame.exc_frame = NULL;

    // Shadow variable for retained RAM variable.
    backtrace_t backtrace = {0};

    // The first two addresses will be those of this function and
    // esp_backtrace_get_start(), so we discard them
    uint8_t count = 0;
    uint8_t stored = 0;
    while ((frame.next_pc != 0) && (stored < FGR_UTIL_ARRAY_LENGTH(backtrace.address_list))) {
        if (count >= 2) {
            // Strip the hardware window bits (top 2 bits) and map to the
            // actual ESP32-S3 instruction space (0x40000000)
            backtrace.address_list[stored] = (frame.pc & 0x3FFFFFFF) | 0x40000000;
            stored++;
        }
        if (!esp_backtrace_get_next_frame(&frame)) {
            break;
        }
        count++;
    }

    backtrace.length = stored;
    if (backtrace.length > 0) {
        // Add the hash of the currently running image
        const esp_app_desc_t *app_desc = esp_app_get_description();
        memcpy(backtrace.hash, app_desc->app_elf_sha256, sizeof(backtrace.hash));
    }

    // Commit to retained RAM
    if (stored > 0) {
        FGR_RRAM_SET(backtrace);
    }

    // Pass control back to the real ESP-IDF panic handler
    __real_esp_panic_handler(param);
}

// Get backtrace.
int32_t fgr_debug_panic_get(uint32_t *backtrace_copy, char *hash)
{
    int32_t length = 0;
    backtrace_t backtrace;

    if (FGR_RRAM_GET(backtrace) == ESP_OK) {
        length = backtrace.length;
        hash_str(hash, backtrace.hash);
        if (backtrace_copy) {
            memcpy(backtrace_copy, backtrace.address_list, length * sizeof(backtrace.address_list[0]));
            FGR_RRAM_CLEAR(backtrace);
        }
    }

    return length;
}

// Populates a buffer with a backtrace as a string.
int32_t fgr_debug_panic_str_get(char *backtrace_str, char *hash)
{
    int32_t length = fgr_debug_panic_get(NULL, NULL);
    int32_t length_str = length * FGR_DEBUG_BACKTRACE_NUMBER_LENGTH;

    if ((length > 0) && backtrace_str) {
        uint32_t backtrace[length];
        fgr_debug_panic_get(backtrace, hash);
        char *p = backtrace_str;
        for (size_t x = 0; x < length; x++) {
            snprintf(p, FGR_DEBUG_BACKTRACE_NUMBER_LENGTH + 1,
                     FGR_DEBUG_BACKTRACE_FORMAT_STRING, (int) backtrace[x]);
            p += FGR_DEBUG_BACKTRACE_NUMBER_LENGTH;
        }
    }

    return length_str;
}

// Log a backtrace.
int32_t fgr_debug_panic_log(const char *tag, const char *prefix,
                            esp_log_level_t level)
{
    int32_t err = ESP_OK;

    int32_t length = fgr_debug_panic_str_get(NULL, NULL);
    if (length > 0) {
        err = -ESP_ERR_NO_MEM;
        char *buffer = MALLOC(length + 1);  // +1 for terminator
        char *hash = MALLOC(FGR_DEBUG_HASH_BUFFER_LENGTH);
        if (buffer && hash) {
            fgr_debug_panic_str_get(buffer, hash);
            debug_log(tag, prefix, hash, level);
            debug_log(tag, prefix, buffer, level);
            err = 1;
        }
        FREE(buffer);
        FREE(hash);
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: STACK OVERFLOW
 * -------------------------------------------------------------- */

// Stack overflow callback.
void vApplicationStackOverflowHook(TaskHandle_t xTask, char *pcTaskName)
{
    // Don't want to put a shadow variable on the stack in this
    // case, so call the fgr_rram function directly.

    size_t length = strlen(pcTaskName);

    if (length > FGR_UTIL_TASK_NAME_MAX_LENGTH - 1) {
        length = FGR_UTIL_TASK_NAME_MAX_LENGTH - 1;
    }
    length++; // Include the terminator in the copy

    // This is safe as "name" is at the start of stack_overflow_task_t.
    fgr_rram_set(pcTaskName, length, &g_stack_overflow_task_rr_container,
                 sizeof(g_stack_overflow_task_rr_container));
}

// Get the name of a task that had a stack overflow.
int32_t fgr_debug_stack_overflow_get(char *buffer)
{
    int32_t length = 0;
    stack_overflow_task_t stack_overflow_task = {0};

    if (FGR_RRAM_GET(stack_overflow_task) == ESP_OK) {
        length = strlen(stack_overflow_task.name);
        if (buffer && (length > 0)) {
            strlcpy(buffer, stack_overflow_task.name,
                    FGR_UTIL_TASK_NAME_MAX_LENGTH);
            FGR_RRAM_CLEAR(stack_overflow_task);
        }
    }

    return length;
}

// Log the name of a task that had a stack overflow.
int32_t fgr_debug_stack_overflow_log(const char *tag, const char *prefix,
                                     esp_log_level_t level)
{
    int32_t err = ESP_OK;
    char buffer[FGR_UTIL_TASK_NAME_MAX_LENGTH];

    int32_t length = fgr_debug_stack_overflow_get(buffer);
    if (length > 0) {
        debug_log(tag, prefix, buffer, level);
        err = 1;
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: CORE DUMP
 * -------------------------------------------------------------- */

// Send a core dump to logging.
int32_t fgr_debug_core_dump_get(const char *tag, esp_log_level_t level)
{
    int32_t err = ESP_OK;
    size_t address = 0;
    size_t length = 0;

    // 1. Locate the physical image details in flash
    if (esp_core_dump_image_get(&address, &length) == ESP_OK && length > 0) {
        err = -ESP_ERR_NOT_FOUND;

        // 2. Fetch the corresponding partition struct
        const esp_partition_t *partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
                                                                    ESP_PARTITION_SUBTYPE_DATA_COREDUMP,
                                                                    NULL);
        if (partition) {
            err = -ESP_ERR_NO_MEM;
            if (!tag) {
                tag = TAG;
            }

            uint8_t *buffer = (uint8_t *) MALLOC(CORE_DUMP_CHUNK_LENGTH);
            char *base64 = (char *) MALLOC(CORE_DUMP_BASE64_CHUNK_LENGTH_MBEDTLS);

            if (buffer && base64) {
                err = 1; // Core dump successfully located and ready
                size_t count = 0;

                // Calculate the true offset inside the partition where the image begins
                size_t offset = address - partition->address;

                debug_log(tag, NULL, "================ CORE DUMP START ================", level);

                int32_t err = 0;
                while ((count < length) && (err == 0)) {
                    size_t read = length - count;
                    if (read > CORE_DUMP_CHUNK_LENGTH) {
                        read = CORE_DUMP_CHUNK_LENGTH;
                    }

                    // Read data relative to the partition boundary
                    esp_partition_read(partition, offset + count, buffer, read);

                    // Process base64 chunk
                    size_t written = 0;
                    err = mbedtls_base64_encode((unsigned char *) base64,
                                                CORE_DUMP_BASE64_CHUNK_LENGTH_MBEDTLS,
                                                &written, buffer, read);
                    if (err == 0) {
                        debug_log(tag, NULL, base64, level);
                    } else {
                        ESP_LOGE(TAG, "Base64 encoding failed with error %d", err);
                        err = -ESP_FAIL;
                    }
                    count += read;
                }

                debug_log(tag, NULL, "================ CORE DUMP END ================", level);
            }

            // Always clear the flag so we do not loop panics dynamically on boot
            esp_core_dump_image_erase();

            FREE(base64);
            FREE(buffer);
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

// Log the ESP32 reset reason.
void fgr_debug_reset_reason_log()
{
    esp_reset_reason_t reset_reason = esp_reset_reason();
    const char *name = "DONT_KNOW_THIS_ONE";

    if ((reset_reason >= 0) && (reset_reason < FGR_UTIL_ARRAY_LENGTH(g_esp_reset_reason))) {
        name = g_esp_reset_reason[reset_reason];
    }
    ESP_LOGI(TAG, "Last reset reason was ESP_RESET_%s (%d)", name, reset_reason);
}

// A message receive handler callback.
bool fgr_debug_msg_receive_handler_cb(fgr_msg_t *msg, void *param)
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
            case FGR_REQ_CNF_DEBUG_LED_OFF:
                fgr_debug_led_off();
                msg_error = FGR_ERROR_NONE;
                break;
            case FGR_REQ_CNF_DEBUG_LED_ON:
                fgr_debug_led_on();
                msg_error = FGR_ERROR_NONE;
                break;
            case FGR_REQ_CNF_DEBUG_LED_BREATHE_OFF:
                fgr_debug_led_breathe_off();
                msg_error = FGR_ERROR_NONE;
                break;
            case FGR_REQ_CNF_DEBUG_LED_BREATHE_ON:
                fgr_debug_led_breathe_on();
                msg_error = FGR_ERROR_NONE;
                break;
            case FGR_REQ_CNF_DEBUG_LED_STATUS:
                // Contents should be one uint8_t
                // representing the bool of LED
                // on/off and another representing
                // the bool of LED breathe on/off
                CONTEXT_LOCK(g_context.lock, "fgr_debug_msg_receive_handler_cb()");
                contents[0] = !g_context.led_masked_off;
                contents[1] = g_context.breathe_enabled;
                length = 2;
                CONTEXT_UNLOCK(g_context.lock, "fgr_debug_msg_receive_handler_cb()");
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

// Print out our MAC address.
void fgr_debug_print_mac_address()
{
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        ESP_LOGI(TAG, "MAC address %02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4],
                 mac[5]);
    }
}

// Get the hash of the current firmware version as a string.
void fgr_debug_get_hash(char *buffer)
{
    // Get description of the currently running app
    const esp_app_desc_t *app_desc = esp_app_get_description();
    hash_str(buffer, app_desc->app_elf_sha256);
}

// Create a hex dump of data in a provided buffer
// (written by DeepSeek 'cos I couldn't find my own
// hex print routine and got lazy).
int32_t fgr_debug_hex_dump_to_buffer(const void *data, size_t data_size,
                                     char *output, size_t output_size)
{
    const unsigned char *bytes = (const unsigned char *)data;
    char *out_ptr = output;
    size_t remaining = output_size;
    int32_t total_written = 0;

    if (output_size == 0) {
        return -1;
    }

    for (size_t i = 0; i < data_size; i += 16) {
        int32_t line_written;

        // Print offset (8 hex digits + space)
        line_written = snprintf(out_ptr, remaining, "%08zx  ", i);
        if (line_written < 0 || (size_t)line_written >= remaining) {
            output[output_size - 1] = '\0';
            return -1;
        }
        out_ptr += line_written;
        remaining -= line_written;
        total_written += line_written;

        // Print hex bytes
        for (size_t j = 0; j < 16; j++) {
            if (i + j < data_size) {
                line_written = snprintf(out_ptr, remaining, "%02x ", bytes[i + j]);
            } else {
                line_written = snprintf(out_ptr, remaining, "   ");
            }

            if (line_written < 0 || (size_t)line_written >= remaining) {
                output[output_size - 1] = '\0';
                return -1;
            }
            out_ptr += line_written;
            remaining -= line_written;
            total_written += line_written;

            // Add extra space in the middle
            if (j == 7) {
                if (remaining < 1) {
                    output[output_size - 1] = '\0';
                    return -1;
                }
                *out_ptr++ = ' ';
                remaining--;
                total_written++;
            }
        }

        // Print ASCII representation
        line_written = snprintf(out_ptr, remaining, " |");
        if (line_written < 0 || (size_t)line_written >= remaining) {
            output[output_size - 1] = '\0';
            return -1;
        }
        out_ptr += line_written;
        remaining -= line_written;
        total_written += line_written;

        for (size_t j = 0; j < 16 && i + j < data_size; j++) {
            unsigned char c = bytes[i + j];
            if (remaining < 1) {
                output[output_size - 1] = '\0';
                return -1;
            }
            *out_ptr++ = isprint(c) ? c : '.';
            remaining--;
            total_written++;
        }

        // Close ASCII section and add newline
        line_written = snprintf(out_ptr, remaining, "|\n");
        if (line_written < 0 || (size_t)line_written >= remaining) {
            output[output_size - 1] = '\0';
            return -1;
        }
        out_ptr += line_written;
        remaining -= line_written;
        total_written += line_written;
    }

    // Ensure null termination
    if (remaining > 0) {
        *out_ptr = '\0';
    } else {
        output[output_size - 1] = '\0';
        return -1;
    }

    return total_written;
}

// End of file
