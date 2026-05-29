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
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_log.h"
#include "errno.h"
#include "ctype.h"
#include "esp_mac.h"
#include "driver/gpio.h"
#include "driver/spi_common.h"
#include "driver/spi_master.h"
#include "esp_core_dump.h"
#include "esp_partition.h"
#include "mbedtls/base64.h"

#include "fgr_util.h"
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

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "debug"

#ifndef FGR_DEBUG_TASK_LED_STACK_SIZE
// Stack size for the task that "breathes" the LED.
#  define FGR_DEBUG_TASK_LED_STACK_SIZE (1024 * 4)
#endif

#ifndef NVS_NAME_LED_MASKED
// A name for the field that masks the LED off in NV storage.
#  define NVS_NAME_LED_MASKED "led_masked"
#endif

#ifndef NVS_NAME_LED_BREATHE_ENABLED
// A name for the field that enables LED "breathing" in NV storage.
// Note: not "led_breathe_enabled" as that turns out to be too long.
#  define NVS_NAME_LED_BREATHE_ENABLED "led_breathe_on"
#endif

#ifndef LED_STEP_DURATION_MS
// How often the LED is reprogrammed: 20 ms is 50 Hz.
#  define LED_STEP_DURATION_MS 20
#endif

#ifndef LED_UPDATE_BREATHE_PERIOD_STEPS
// The number of steps, of duration LED_STEP_DURATION_MS, that
// constitute a "breath", i.e. one cycle of breating.
// 200 steps * 20ms = 4000ms (4 seconds)
#  define LED_UPDATE_BREATHE_PERIOD_STEPS 200
#endif

#ifndef FLASH_INTENSITY_BOOST_NUMERATOR
// Flash must be at least this much brighter than breath peak
// Value is numerator of a fraction with denominator 100 (e.g., 50 = 50% brighter)
#  define FLASH_INTENSITY_BOOST_NUMERATOR 50
#endif

#ifndef FLASH_MIN_INTENSITY
// Minimum intensity for any flash when breathing (0-255)
#  define FLASH_MIN_INTENSITY 128
#endif

// The maximum intensity - range is 0 to 255 to match
// the WS2812 encoded brightess range for each of R, G and B.
#define INTENSITY_SCALE_MAX 255

// The WS2812 tri-colour LED used for the CONFIG_FGR_DEBUG_LED_SPI_NUM case,
// see datasheet here:
//
// https://www.normandled.com/upload/201607/WS2812B%20Mini%203535%20LED%20Datasheet.pdf
//
// ...is driven over a single wire as follows:
//
// Zero bit high for 400 ns +/- 150 ns
// Zero bit low for 850 ns +/- 150 ns
// One bit high for 800 ns +/- 150 ns
// One bit low for 450 ns +/- 150 ns
// Meaning of bits is 8 bits red then 8 bits green then 8 bits blue
// or 8 bits green then 8 bits red then 8 bits blue if CONFIG_FGR_DEBUG_LED_WS2812_GRB
// Order of transmission is MSB first, as is SPI
// End of group timing is to go low for > 50000 ns
//
//
// Therefore the frequency of transmission is 800 kHz
// (1000000000 / (400 + 850)) but we need to obey the specific
// timings for each bit, hence we run at 8 MHz, so 10 SPI bits
// per WS2812 bit, and send more 1's and 0's to meet the timings.
#define SPI_SPEED_HZ 8000000

// Given an SPI speed of 8 MHz, each SPI bit is 125 ns, so a
// good number of SPI bits high to represent a zero (400 ns)
// is 3 (3 * 125 ns = 375 ns).  The remaining bits, are zero
// so that is 11100000 (given SPI transmits MSB first) plus
// two bits of 00 to give a total of 10 making 1250 ns.
#define WS2812_ZERO 0xe0

// Similarly a good number of SPI bits high to represent a one
// (800 ns) is 6 (6 * 125 ns = 750 ns), so 11111100 plus two
// bits of 00.
#define WS2812_ONE 0xfc

// The number of SPI bits per WS2812 bit
#define SPI_BITS_PER_WS2812_BIT 10

// The number of SPI bytes per WS2812 byte
#define SPI_BYTES_PER_WS2812_BYTE 10

// The number of SPI bits per WS2812 byte
#define SPI_BITS_PER_WS2812_BYTE (SPI_BITS_PER_WS2812_BIT * 8)

// The low time to add on the end to signal end of group
#define WS2812_END_OF_GROUP_SPI_BITS_LOW (51000 / (1000000000 / SPI_SPEED_HZ))

// The number of SPI bits per WS2812 RGB/GRB transaction: 3
// WS2812 bytes plus the end of group low time
#define SPI_BITS_PER_WS2812_TRANSACTION ((SPI_BITS_PER_WS2812_BYTE * 3) + WS2812_END_OF_GROUP_SPI_BITS_LOW)

// The buffer size (in bytes) to hold a WS2812 RGB/GRB transaction
#define SPI_TRANSACTION_BUFFER_LENGTH_BYTES ((SPI_BITS_PER_WS2812_TRANSACTION / 8) + 1)

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

// LED mode.
typedef enum {
    FGR_LED_MODE_OFF,
    FGR_LED_MODE_BREATHE,
    FGR_LED_MODE_FLASH
} fgr_led_mode_t;

// Breathing state.
typedef struct {
    bool enabled;
    bool use_cb; // If true, set the breathe colour based on then node's state, via the callback
    fgr_debug_colour_t colour;
    size_t period_steps;     // Period in steps (each step = LED_STEP_DURATION_MS)
    size_t step_counter;     // Current step in breathing cycle (0 to period_steps-1)
    uint8_t intensity;       // Current breathing intensity (0-255)
} breathe_state_t;

// Flash state.
typedef struct {
    bool active;
    fgr_debug_colour_t colour;
    size_t total_steps;      // Total flash duration in steps
    size_t step_counter;     // Current step (0 to total_steps-1)
    bool completed;          // Flash has finished
} flash_state_t;

// The command structure to be transferred on a queue to the LED task.
typedef struct {
    fgr_led_mode_t mode;
    fgr_debug_colour_t colour;
    size_t flash_duration_steps;  // Duration in steps (each step = LED_STEP_DURATION_MS)
    size_t breathe_period_steps;  // Period in steps
} led_cmd_t;

// Context.
typedef struct {
    spi_device_handle_t spi;
    SemaphoreHandle_t lock;
    TaskHandle_t task_handle;
    QueueHandle_t queue_handle;
    fgr_debug_state_cb_t cb;
    void *cb_param;
    bool led_masked_off;
    breathe_state_t breathe_state;
    flash_state_t flash_state;
} context_t;

// Storage for a backtrace.
typedef struct {
    uint8_t length;
    uint32_t address_list[FGR_DEBUG_BACKTRACE_DEPTH_MAX];
} backtrace_t;

// Storage for an overflowing task name.
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

// Sine lookup table: values from 0 to INTENSITY_SCALE_MAX.
static const uint8_t g_sine_table[] = {
    128, 140, 153, 165, 177, 188, 199, 209,
    218, 226, 234, 240, 245, 250, 253, 255,
    255, 255, 253, 250, 245, 240, 234, 226,
    218, 209, 199, 188, 177, 165, 153, 140,
    128, 115, 102,  90,  78,  67,  56,  46,
    37,  29,  21,  15,  10,   5,   2,   0,
    0,   0,   2,   5,  10,  15,  21,  29,
    37,  46,  56,  67,  78,  90, 102, 115
};

// Flash ease table: 0 to INTENSITY_SCALE_MAX and back to 0 over
// 32 steps, using a sine-like ease curve for soft edges
static const uint8_t g_flash_ease_table[] = {
    0,   1,   4,   8,  13,  19,  26,  34,
    42,  51,  61,  71,  82,  93, 104, 115,
    126, 137, 148, 159, 170, 180, 190, 199,
    208, 216, 224, 231, 237, 242, 246, 249,
    252, 254, 255, 255, 254, 252, 249, 246,
    242, 237, 231, 224, 216, 208, 199, 190,
    180, 170, 159, 148, 137, 126, 115, 104,
    93,  82,  71,  61,  51,  42,  34,  26,
    19,  13,   8,   4,   1,   0
};

// Table of states to breathe colours
static const fgr_debug_colour_t g_state_to_breathe_colour[] = {FGR_DEBUG_LED_COLOUR_BOOT,      // FGR_STATE_NOT_POPULATED (0)
                                                               FGR_DEBUG_LED_COLOUR_NEEDS_CFG, // FGR_STATE_NEEDS_CFG (1)
                                                               FGR_DEBUG_LED_COLOUR_GOOD,      // FGR_STATE_STARTED (2)
                                                               FGR_DEBUG_LED_COLOUR_STOPPED,   // FGR_STATE_STOPPED (3)
                                                               FGR_DEBUG_LED_COLOUR_BAD,       // FGR_STATE_DISCONNECTED (4)
                                                               FGR_DEBUG_LED_COLOUR_BAD,       // FGR_STATE_GENERIC_FAILED (5)
                                                               FGR_DEBUG_LED_COLOUR_BAD
                                                              };      // FGR_STATE_HARDWARE_FAILURE (6)

#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

// Storage for backtrace address list in retained RAM.
FGR_RRAM_DEFINE(backtrace_t, backtrace);

// Storage for an overflowing stack's name in retained RAM.
FGR_RRAM_DEFINE(stack_overflow_task_t, stack_overflow_task);

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS: WS2812 RELATED
 * -------------------------------------------------------------- */

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

// Encode a WS2812 bit, spanning 10 SPI bits, into an SPI buffer.
// Returns the number of bytes written (1-3) and advances the buffer pointer.
static inline size_t ws2812_encode_bit(bool oneNotZero, int32_t bit_offset,
                                       uint8_t **buffer_ptr, size_t length)
{
    uint32_t bit_pattern = oneNotZero ? WS2812_ONE : WS2812_ZERO;
    size_t bytes_encoded = 0;

    if (buffer_ptr && *buffer_ptr && (length > 0)) {
        // Shift to align with MSB-first SPI order
        bit_pattern <<= 24;
        bit_pattern >>= bit_offset;

        // Encode first byte (may have partial bits from previous)
        if (bit_offset > 0) {
            uint8_t mask = 0xff >> bit_offset;  // Keep bits from MSB down to offset
            **buffer_ptr = (**buffer_ptr & ~mask) | ((bit_pattern >> 24) & mask);
        } else {
            **buffer_ptr = bit_pattern >> 24;
        }
        (*buffer_ptr)++;
        bytes_encoded++;

        // Write remaining full bytes (if space permits)
        if (bytes_encoded < length) {
            **buffer_ptr = bit_pattern >> 16;
            (*buffer_ptr)++;
            bytes_encoded++;
        }
        if (bytes_encoded < length) {
            **buffer_ptr = bit_pattern >> 8;
            bytes_encoded++;
        }
    }

    return bytes_encoded;
}

// Encode a WS2812 byte into SPI buffer.
static inline void ws2812_encode_byte(uint8_t byte_ws2812,
                                      uint8_t **buffer_ptr,
                                      size_t *length_ptr)
{
    uint8_t bit_offset = 0;

    if (buffer_ptr && *buffer_ptr && length_ptr) {
        for (int32_t x = 7; (x >= 0) && (*length_ptr > 0); x--) {
            size_t encoded = ws2812_encode_bit(byte_ws2812 & (1 << x),
                                               bit_offset, buffer_ptr,
                                               *length_ptr);
            *length_ptr -= encoded;
            bit_offset += SPI_BITS_PER_WS2812_BIT;

            while (bit_offset >= 8) {
                bit_offset -= 8;
                // buffer already advanced by ws2812_encode_bit
            }
        }
    }
}

// Assemble a buffer of SPI data to represent a WS2812 transaction.
static size_t ws2812_spi_transaction(fgr_debug_colour_t *colour,
                                     uint8_t *buffer, size_t length)
{
    size_t encoded_length_bits = 0;

    if (colour && buffer && (length >= SPI_TRANSACTION_BUFFER_LENGTH_BYTES)) {
        // Zero the buffer first
        memset(buffer, 0, SPI_TRANSACTION_BUFFER_LENGTH_BYTES);

        uint8_t *buffer_ptr = buffer;
        // Encode the three bytes, order RGB
#ifdef CONFIG_FGR_DEBUG_LED_WS2812_GRB
        ws2812_encode_byte(colour->green, &buffer_ptr, &length);
        ws2812_encode_byte(colour->red, &buffer_ptr, &length);
#else
        ws2812_encode_byte(colour->red, &buffer_ptr, &length);
        ws2812_encode_byte(colour->green, &buffer_ptr, &length);
#endif
        ws2812_encode_byte(colour->blue, &buffer_ptr, &length);
        encoded_length_bits = SPI_BITS_PER_WS2812_TRANSACTION;
    }

    // Return the total bit length (including reset bits)
    return encoded_length_bits;
}

#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

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
 * STATIC FUNCTIONS: LED TASK AND RELATED
 * -------------------------------------------------------------- */

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

// Apply intensity to colour.
static fgr_debug_colour_t apply_intensity(fgr_debug_colour_t colour,
                                          uint8_t intensity)
{
    fgr_debug_colour_t result;

    // (colour * intensity) / 255 - all integer math
    result.red = (uint16_t)colour.red * intensity / 255;
    result.green = (uint16_t)colour.green * intensity / 255;
    result.blue = (uint16_t)colour.blue * intensity / 255;

    return result;
}

// Calculate boosted flash colour to ensure it is noticeably brighter than breath.
static void boost_flash_intensity(fgr_debug_colour_t *flash_colour,
                                  fgr_debug_colour_t breath_colour,
                                  uint8_t breath_intensity)
{

    if (flash_colour) {
        fgr_debug_colour_t result = {0, 0, 0};

        // Calculate the current maximum brightness of any channel in the breath
        uint16_t breath_peak = 0;
        uint16_t breath_red = ((uint16_t) breath_colour.red) * breath_intensity / 255;
        uint16_t breath_green = ((uint16_t) breath_colour.green) * breath_intensity / 255;
        uint16_t breath_blue = ((uint16_t) breath_colour.blue) * breath_intensity / 255;

        if (breath_red > breath_peak) {
            breath_peak = breath_red;
        }
        if (breath_green > breath_peak) {
            breath_peak = breath_green;
        }
        if (breath_blue > breath_peak) {
            breath_peak = breath_blue;
        }

        // Calculate required minimum intensity for flash: breath_peak * (100 + boost) / 100
        uint16_t required_intensity = breath_peak * (100 + FLASH_INTENSITY_BOOST_NUMERATOR) / 100;
        if (required_intensity > 255) {
            required_intensity = 255;
        }
        if (required_intensity < FLASH_MIN_INTENSITY) {
            required_intensity = FLASH_MIN_INTENSITY;
        }

        // Find the maximum channel value in the requested flash colour
        uint16_t flash_max = 0;
        if (flash_colour->red > flash_max) {
            flash_max = flash_colour->red;
        }
        if (flash_colour->green > flash_max) {
            flash_max = flash_colour->green;
        }
        if (flash_colour->blue > flash_max) {
            flash_max = flash_colour->blue;
        }

        if (flash_max > 0) {
            // Scale all channels proportionally: (colour * required) / flash_max
            result.red = ((uint16_t) flash_colour->red) * required_intensity / flash_max;
            result.green = ((uint16_t) flash_colour->green) * required_intensity / flash_max;
            result.blue = ((uint16_t) flash_colour->blue) * required_intensity / flash_max;
        } else {
            // Flash colour is black - use pure white at required intensity
            result.red = required_intensity;
            result.green = required_intensity;
            result.blue = required_intensity;
        }

        *flash_colour = result;
    }
}

// Convert an fgr_state_t into the corresponding set of fgr_debug_colour_t.
static fgr_debug_colour_t fgr_state_to_colour(fgr_state_t state)
{
    // This should come out as a dull orange if the mapping fails
    fgr_debug_colour_t colour = {200, 120, 0};

    if (state < FGR_UTIL_ARRAY_LENGTH(g_state_to_breathe_colour)) {
        colour = g_state_to_breathe_colour[state];
    }

    return colour;
}

// Update breathe colour based on the state of the node.
// IMPORTANT: the context should be locked before this is called.
static void update_breathe_colour(context_t *context)
{
    breathe_state_t *breathe_state = &(context->breathe_state);
    fgr_state_t state = FGR_STATE_NOT_POPULATED;

    if (context->cb) {
        state = context->cb(context->cb_param);
    }
    if (state <= FGR_STATE_LAST && (breathe_state->enabled)) {
        breathe_state->colour = fgr_state_to_colour(state);
        breathe_state->period_steps = LED_UPDATE_BREATHE_PERIOD_STEPS;
    }
}

// Update breathing intensity using lookup table.
// IMPORTANT: the context should be locked before this is called.
static void update_breathe_intensity(breathe_state_t *breathe_state)
{
    if (breathe_state->enabled && (breathe_state->period_steps != 0)) {
        // Map step_counter to sine table index
        // We want a full sine wave over the period
        uint32_t index = (breathe_state->step_counter * FGR_UTIL_ARRAY_LENGTH(
                              g_sine_table)) / breathe_state->period_steps;
        index &= FGR_UTIL_ARRAY_LENGTH(g_sine_table) - 1;  // Ensure within bounds
        breathe_state->intensity = g_sine_table[index];

        // Advance step counter
        breathe_state->step_counter++;
        if (breathe_state->step_counter >= breathe_state->period_steps) {
            breathe_state->step_counter = 0;
        }
    }
}

// Update flash intensity using lookup table
// IMPORTANT: the context should be locked before this is called.
static void update_flash_intensity(flash_state_t *flash_state,
                                   fgr_debug_colour_t *colour)
{
    if (flash_state->active && !flash_state->completed && (flash_state->total_steps > 0)) {

        uint32_t index = (flash_state->step_counter * FGR_UTIL_ARRAY_LENGTH(g_flash_ease_table)) /
                         flash_state->total_steps;
        uint8_t intensity = g_flash_ease_table[index];
        *colour = apply_intensity(flash_state->colour, intensity);

        flash_state->step_counter++;
        if (flash_state->step_counter >= flash_state->total_steps) {
            flash_state->active = false;
            flash_state->completed = true;
        }
    }
}

// Update physical WS2812 LED.
// IMPORTANT: the context should be locked before this is called.
static void update_led(context_t *context)
{
    breathe_state_t *breathe_state = &(context->breathe_state);
    flash_state_t *flash_state = &(context->flash_state);
    fgr_debug_colour_t final_colour = {0, 0, 0};
    fgr_debug_colour_t temp_colour;

    // Priority order: Mask > Flash > Breathe > Off
    if (context->led_masked_off) {
        final_colour = FGR_DEBUG_LED_COLOUR_NONE;
    } else if (flash_state->active) {
        temp_colour = flash_state->colour;
        update_flash_intensity(flash_state, &temp_colour);
        final_colour = temp_colour;
    } else if (breathe_state->enabled) {
        final_colour = apply_intensity(breathe_state->colour, breathe_state->intensity);
    } else {
        final_colour = FGR_DEBUG_LED_COLOUR_NONE;
    }

    // Perform SPI transaction
    if (context->spi) {
        uint8_t *buffer = (uint8_t *)heap_caps_malloc(SPI_TRANSACTION_BUFFER_LENGTH_BYTES,
                                                      MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
        if (buffer) {
            spi_transaction_t transaction = {0};
            transaction.length = ws2812_spi_transaction(&final_colour, buffer,
                                                        SPI_TRANSACTION_BUFFER_LENGTH_BYTES);
            transaction.tx_buffer = buffer;
            spi_device_transmit(context->spi, &transaction);
            heap_caps_free(buffer);
        }
    }
}

// LED task callback
static void task_led_cb(void *handle, void *param)
{
    context_t *context = (context_t *) param;
    breathe_state_t *breathe_state = &(context->breathe_state);
    flash_state_t *flash_state = &(context->flash_state);
    static uint32_t last_update_ms = 0;
    led_cmd_t cmd;

    (void) handle;

    uint32_t now_ms = xTaskGetTickCount() * portTICK_PERIOD_MS;

    CONTEXT_LOCK(g_context.lock, "task_led_cb()");

    // Process all pending messages (non-blocking)
    while (xQueueReceive(context->queue_handle, &cmd, 0) == pdTRUE) {
        switch (cmd.mode) {
            case FGR_LED_MODE_BREATHE:
                if ((cmd.colour.red == 0) && (cmd.colour.green == 0) && (cmd.colour.blue == 0)) {
                    // Receiving a command with all zeroes for the colours
                    // indicates that we should should choose the breathe
                    // colour automatically based on the state of the node
                    if (context->cb) {
                        breathe_state->use_cb = true;
                    }
                } else {
                    breathe_state->use_cb = false;
                }
                breathe_state->colour = cmd.colour;
                breathe_state->period_steps = cmd.breathe_period_steps;
                breathe_state->step_counter = 0;
                break;

            case FGR_LED_MODE_FLASH:
                flash_state->active = true;
                if (breathe_state->enabled) {
                    // Make flash more visible if breathing
                    boost_flash_intensity(&cmd.colour,
                                          breathe_state->colour,
                                          breathe_state->intensity);
                }
                flash_state->colour = cmd.colour;
                flash_state->total_steps = cmd.flash_duration_steps;
                flash_state->step_counter = 0;
                flash_state->completed = false;
                break;

            case FGR_LED_MODE_OFF:
                breathe_state->enabled = false;
                flash_state->active = false;
                break;

            default:
                break;
        }
    }

    // Update breathe colour at the start of every breath if we're using the callback
    if (breathe_state->use_cb && (breathe_state->intensity == 0)) {
        update_breathe_colour(context);
    }
    // Update LED at consistent intervals
    if ((now_ms - last_update_ms) >= LED_STEP_DURATION_MS) {
        update_breathe_intensity(breathe_state);
        update_led(context);
        last_update_ms = now_ms;
    }

    CONTEXT_UNLOCK(g_context.lock, "task_led_cb()");
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

        CONTEXT_LOCK(g_context.lock, "led_masked_off()");
        context->led_masked_off = masked;
        nvs_led_masked_set(masked);
        CONTEXT_UNLOCK(g_context.lock, "led_masked_off()");
    }
}

// Set whether LED "breathing" is enabled.
static void led_breathe_enabled(context_t *context, bool enabled)
{
    if (context->lock) {

        CONTEXT_LOCK(g_context.lock, "led_breathe_enabled()");

        if (context->queue_handle) {

            context->breathe_state.enabled = enabled;
            nvs_led_breathe_enabled_set(enabled);
            led_cmd_t cmd;
            if (enabled) {
                // Resume breathing with last colour
                cmd.mode = FGR_LED_MODE_BREATHE;
                cmd.colour = context->breathe_state.colour;
                cmd.breathe_period_steps = context->breathe_state.period_steps;
                cmd.flash_duration_steps = 0;
            } else {
                cmd.mode = FGR_LED_MODE_OFF;
            }

            xQueueSend(context->queue_handle, &cmd, portMAX_DELAY);
        }

        CONTEXT_UNLOCK(g_context.lock, "led_breathe_enabled()");
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

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: INITIALISE/DEINITIALISE
 * -------------------------------------------------------------- */

// Initialise debug stuff.
int32_t fgr_debug_init(fgr_debug_state_cb_t cb, void *cb_param)
{
    esp_err_t err = ESP_ERR_NO_MEM;

    if (!g_context.lock) {
        g_context.lock = xSemaphoreCreateMutex();
    }

    if (g_context.lock) {
        err = ESP_OK;

        if (!g_context.queue_handle) {

            CONTEXT_LOCK(g_context.lock, "fgr_debug_init()");

            g_context.led_masked_off = false;
            g_context.breathe_state.enabled = true;

            // Read values from non-volatile storage and,
            // if not present, write the default value back
            if (nvs_led_masked_get(&g_context.led_masked_off) != ESP_OK) {
                nvs_led_masked_set(g_context.led_masked_off);
            }
            if (nvs_led_breathe_enabled_get(&g_context.breathe_state.enabled) != ESP_OK) {
                nvs_led_breathe_enabled_set(g_context.breathe_state.enabled);
            }

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

            // Use SPI to clock out the 24 bits of RGB/GRB to a WS2812 LED.
            spi_bus_config_t bus_cfg = {
                .mosi_io_num = CONFIG_FGR_DEBUG_LED_PIN,
                .miso_io_num = -1,
                .sclk_io_num = -1,
                .quadwp_io_num = -1,
                .quadhd_io_num = -1
            };

            spi_device_interface_config_t dev_cfg = {
                .clock_speed_hz = SPI_SPEED_HZ,
                .mode = 0,
                .spics_io_num = -1,    // No chip select
                .queue_size = 7
            };

            err = spi_bus_initialize(CONFIG_FGR_DEBUG_LED_SPI_NUM, &bus_cfg, SPI_DMA_CH_AUTO);
            if (err == ESP_OK) {
                err = spi_bus_add_device(CONFIG_FGR_DEBUG_LED_SPI_NUM, &dev_cfg, &g_context.spi);
                if (err == ESP_OK) {
                    err = ESP_ERR_NO_MEM;
                    g_context.queue_handle = xQueueCreate(10, sizeof(led_cmd_t));
                    if (g_context.queue_handle) {
                        err = fgr_task_create(&task_led_cb, &g_context, "led",
                                              FGR_DEBUG_TASK_LED_STACK_SIZE,
                                              3, &g_context.task_handle);
                        if (err == ESP_OK) {
                            // Only now add the callback, when we know we can use it
                            g_context.cb = cb;
                            g_context.cb_param = cb_param;
                            if (cb) {
                                g_context.breathe_state.use_cb = true;
                            }
                            ESP_LOGI(TAG,
                                     "If the LED breathes red when obviously connected, toggle CONFIG_FGR_DEBUG_LED_WS2812_GRB.");
                        } else {
                            vQueueDelete(g_context.queue_handle);
                            g_context.queue_handle = NULL;
                        }
                    }

                } else {
                    spi_bus_free(CONFIG_FGR_DEBUG_LED_SPI_NUM);
                    g_context.spi = NULL; // Just in case
                    ESP_LOGE(TAG, "spi_bus_add_device() to SPI %d failed (%s)!",
                             CONFIG_FGR_DEBUG_LED_SPI_NUM, esp_err_to_name(err));
                }
            } else {
                ESP_LOGE(TAG, "spi_bus_initialize() on SPI %d failed (%s)!",
                         CONFIG_FGR_DEBUG_LED_SPI_NUM, esp_err_to_name(err));
            }
#  else
            // Configure our single colour debug LED
            err = gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);
            if (err == ESP_OK) {
                err = gpio_set_direction(CONFIG_FGR_DEBUG_LED_PIN, GPIO_MODE_OUTPUT);
                if (err == ESP_OK) {
                    // Flash it so that we know it can be active
                    fgr_debug_led_flash(FGR_DEBUG_LED_LONG_MS, FGR_DEBUG_LED_COLOUR_BOOT);
                } else {
                    ESP_LOGE(TAG, "gpio_set_direction() on pin %d failed (%s)!",
                             CONFIG_FGR_DEBUG_LED_PIN, esp_err_to_name(err));
                }
            } else {
                ESP_LOGE(TAG, "gpio_set_level() on pin %d failed (%s)!",
                         CONFIG_FGR_DEBUG_LED_PIN, esp_err_to_name(err));
            }
#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

            CONTEXT_UNLOCK(g_context.lock, "fgr_debug_init()");

            if (err == ESP_OK) {
                // Flash the LED so that we know it can be active
                fgr_debug_led_flash(FGR_DEBUG_LED_LONG_MS, FGR_DEBUG_LED_COLOUR_BOOT);
            }
        }
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) - err;
}

// Deinitialise debug stuff.
void fgr_debug_deinit()
{
#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally
    if (g_context.lock) {

        ESP_LOGI(TAG, "Stopping debug.");

        // Need to do this before taking the lock or we
        // will lock-up the task exit
        fgr_task_destroy(g_context.task_handle);
        g_context.task_handle = NULL;

        CONTEXT_LOCK(g_context.lock, "fgr_debug_deinit()");

        if (g_context.queue_handle) {
            vQueueDelete(g_context.queue_handle);
            g_context.queue_handle = NULL;
        }

        if (g_context.spi) {
            spi_bus_remove_device(g_context.spi);
            spi_bus_free(CONFIG_FGR_DEBUG_LED_SPI_NUM);
            g_context.spi = NULL;
        }

        // Forget any callback
        g_context.breathe_state.use_cb = false;
        g_context.cb = NULL;
        g_context.cb_param = NULL;

        CONTEXT_UNLOCK(g_context.lock, "fgr_debug_deinit()");
        // The semaphore will be re-used
    }
#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: LED RELATED
 * -------------------------------------------------------------- */

// Flash the debug LED.
void fgr_debug_led_flash(int32_t duration_ms, fgr_debug_colour_t colour)
{
    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_debug_led_flash()");

        if (!g_context.led_masked_off) {

#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

            // Convert milliseconds to steps (rounded up)
            uint32_t steps = (duration_ms + LED_STEP_DURATION_MS - 1) / LED_STEP_DURATION_MS;
            if (g_context.queue_handle) {
                led_cmd_t cmd = {
                    .mode = FGR_LED_MODE_FLASH,
                    .colour = colour,
                    .flash_duration_steps = steps,
                    .breathe_period_steps = 0
                };
                xQueueSend(g_context.queue_handle, &cmd, portMAX_DELAY);
            }
#  else
            // Single colour LED
            gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 0);
            vTaskDelay(pdMS_TO_TICKS(duration_ms));
            gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);

#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)

        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_debug_led_flash()");
    }
}

// Set the LED "breathing".
void fgr_debug_led_breathe_set(fgr_debug_colour_t colour)
{
#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally

    if (g_context.lock) {

        CONTEXT_LOCK(g_context.lock, "fgr_debug_set_breathe()");

        if (g_context.queue_handle && !g_context.led_masked_off &&
            g_context.breathe_state.enabled) {
            led_cmd_t cmd = {
                .mode = FGR_LED_MODE_BREATHE,
                .colour = colour,
                .breathe_period_steps = LED_UPDATE_BREATHE_PERIOD_STEPS,
                .flash_duration_steps = 0
            };
            xQueueSend(g_context.queue_handle, &cmd, portMAX_DELAY);

        }

        CONTEXT_UNLOCK(g_context.lock, "fgr_debug_set_breathe()");
    }
#  endif  // #if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#endif    // #  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1)
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

    // Commit to retained RAM
    if (stored > 0) {
        FGR_RRAM_SET(backtrace);
    }

    // Pass control back to the real ESP-IDF panic handler
    __real_esp_panic_handler(param);
}

// Get backtrace.
int32_t fgr_debug_panic_get(uint32_t *backtrace_copy)
{
    int32_t length = 0;
    backtrace_t backtrace;

    if (FGR_RRAM_GET(backtrace) == ESP_OK) {
        length = backtrace.length;
        if (backtrace_copy) {
            memcpy(backtrace_copy, backtrace.address_list, length * sizeof(backtrace.address_list[0]));
            FGR_RRAM_CLEAR(backtrace);
        }
    }

    return length;
}

// Populates a buffer with a backtrace as a string.
int32_t fgr_debug_panic_str_get(char *buffer)
{
    int32_t length = fgr_debug_panic_get(NULL);
    int32_t length_str = length * FGR_DEBUG_BACKTRACE_NUMBER_LENGTH;

    if ((length > 0) && buffer) {
        uint32_t backtrace[length];
        fgr_debug_panic_get(backtrace);
        char *p = buffer;
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

    int32_t length = fgr_debug_panic_str_get(NULL);
    if (length > 0) {
        err = -ESP_ERR_NO_MEM;
        char *buffer = malloc(length);
        if (buffer) {
            fgr_debug_panic_str_get(buffer);
            debug_log(tag, prefix, buffer, level);
            err = 1;
            free(buffer);
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: STACK OVERFLOW
 * -------------------------------------------------------------- */

// Stack overflow callback.
void vApplicationStackOverflowHook(TaskHandle_t xTask, char *pcTaskName)
{
    stack_overflow_task_t stack_overflow_task;
    strlcpy(stack_overflow_task.name, pcTaskName, sizeof(stack_overflow_task.name));
    FGR_RRAM_SET(stack_overflow_task);
}

// Get the name of a task that had a stack overflow.
int32_t fgr_debug_stack_overflow_get(char *buffer)
{
    int32_t length = 0;
    stack_overflow_task_t stack_overflow_task;

    if (FGR_RRAM_GET(stack_overflow_task) == ESP_OK) {
        length = strlen(stack_overflow_task.name);
        if (buffer && (length > 0)) {
            strlcpy(buffer, stack_overflow_task.name,
                    FGR_DEBUG_BACKTRACE_BUFFER_LENGTH);
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

            uint8_t *buffer = (uint8_t *) malloc(CORE_DUMP_CHUNK_LENGTH);
            char *base64 = (char *) malloc(CORE_DUMP_BASE64_CHUNK_LENGTH_MBEDTLS);

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

            free(base64);
            free(buffer);
        }
    }

    return err;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS: MISC
 * -------------------------------------------------------------- */

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
                contents[1] = g_context.breathe_state.enabled;
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
