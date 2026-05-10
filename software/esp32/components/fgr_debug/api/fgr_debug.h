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

#ifndef _FGR_DEBUG_H_
#define _FGR_DEBUG_H_

 /** @file
  * @brief The debug utilities API for a node of the front garden railway.
  */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_DEBUG_LED_SHORT_MS
// Standard short duration for an LED lash.
#  define FGR_DEBUG_LED_SHORT_MS 50
#endif

#ifndef FGR_DEBUG_LED_LONG_MS
// Standard short duration for a long LED flash.
#  define FGR_DEBUG_LED_LONG_MS 1000
#endif

#ifndef FGR_DEBUG_LED_INTENSITY_LOW
// How bright to shine the LED for low intensity; ignored
// for a single colour LED.
#  define FGR_DEBUG_LED_INTENSITY_LOW 64
#endif

#ifndef FGR_DEBUG_LED_INTENSITY_HIGH
// How bright to shine the LED for high intensity; ignored
// for a single colour LED.
#  define FGR_DEBUG_LED_INTENSITY_HIGH 128
#endif

#ifndef FGR_DEBUG_LED_COLOUR_RED
// Red; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_RED ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_LOW, 0, 0})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_RED
// Bright red; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_RED ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_HIGH, 0, 0})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BLUE
// Blue; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BLUE ((fgr_debug_colour_t) {0, 0, FGR_DEBUG_LED_INTENSITY_LOW})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_BLUE
// Bright blue; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_BLUE ((fgr_debug_colour_t) {0, 0, FGR_DEBUG_LED_INTENSITY_HIGH})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_GREEN
// Green; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_GREEN ((fgr_debug_colour_t) {0, FGR_DEBUG_LED_INTENSITY_LOW, 0})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_GREEN
// Bright green; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_GREEN ((fgr_debug_colour_t) {0, FGR_DEBUG_LED_INTENSITY_HIGH, 0})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_YELLOW
// Yellow; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_YELLOW ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_LOW, FGR_DEBUG_LED_INTENSITY_LOW, 0})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_YELLOW
// Bright yellow; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_YELLOW ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_HIGH, FGR_DEBUG_LED_INTENSITY_HIGH, 0})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_CYAN
// Cyan; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_CYAN ((fgr_debug_colour_t) {0, FGR_DEBUG_LED_INTENSITY_LOW, FGR_DEBUG_LED_INTENSITY_LOW})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_CYAN
// Bright cyan; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_CYAN ((fgr_debug_colour_t) {0, FGR_DEBUG_LED_INTENSITY_HIGH, FGR_DEBUG_LED_INTENSITY_HIGH})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_MAGENTA
// Magenta; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_MAGENTA ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_LOW, 0, FGR_DEBUG_LED_INTENSITY_LOW})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_MAGENTA
// Bright magenta; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_MAGENTA ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_HIGH, 0, FGR_DEBUG_LED_INTENSITY_HIGH})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_WHITE
// White; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_WHITE ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_LOW, FGR_DEBUG_LED_INTENSITY_LOW, FGR_DEBUG_LED_INTENSITY_LOW})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BRIGHT_WHITE
// Bright white; generally better to use one of the "meaning" colours below instead of this.
#  define FGR_DEBUG_LED_COLOUR_BRIGHT_WHITE ((fgr_debug_colour_t) {FGR_DEBUG_LED_INTENSITY_HIGH, FGR_DEBUG_LED_INTENSITY_HIGH, FGR_DEBUG_LED_INTENSITY_HIGH})
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BOOT
// Standardised boot colour.
#  define FGR_DEBUG_LED_COLOUR_BOOT FGR_DEBUG_LED_COLOUR_WHITE
#endif

#ifndef FGR_DEBUG_LED_COLOUR_ALARM
// Standardised alarm colour: the only one that is bright.
#  define FGR_DEBUG_LED_COLOUR_ALARM FGR_DEBUG_LED_COLOUR_BRIGHT_RED
#endif

#ifndef FGR_DEBUG_LED_COLOUR_BAD
// Standardised negative colour.
#  define FGR_DEBUG_LED_COLOUR_BAD FGR_DEBUG_LED_COLOUR_RED
#endif

#ifndef FGR_DEBUG_LED_COLOUR_GOOD
// Standardised positive colour.
#  define FGR_DEBUG_LED_COLOUR_GOOD FGR_DEBUG_LED_COLOUR_GREEN
#endif

#ifndef FGR_DEBUG_LED_COLOUR_NOTIFY
// Standardised neutral notification colour.
#  define FGR_DEBUG_LED_COLOUR_NOTIFY FGR_DEBUG_LED_COLOUR_BLUE
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/** Debug LED colour.
 */
typedef struct {
    uint8_t red;
    uint8_t green;
    uint8_t blue;
} fgr_debug_colour_t;

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise debug.
 *
 * @return ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_debug_init();

/** Flash the debug LED.
 *
 * @param duration_ms how long to flash the LED for (e.g.
 *                    FGR_DEBUG_LED_SHORT_MS or FGR_DEBUG_LED_LONG_MS).
 * @param colour      the LED colour, ignored if
 *                    CONFIG_FGR_DEBUG_LED_IS_WS2812 is not defined.
 */
void fgr_debug_flash_led(int32_t duration_ms, fgr_debug_colour_t colour);

/** Deinitialise debug.
 */
void fgr_debug_deinit();

/** Print out our MAC address if possible.
 */
void fgr_debug_print_mac_address();

/** Create a hex dump of data in a provided buffer.
 *
 * @param data          input data to dump
 * @param data_size     size of input data in bytes
 * @param output        output buffer to fill with hex dump
 * @param output_size   size of output buffer
 * @return              number of characters written (excluding
 *                      null terminator), or -1 if output
 *                      buffer is too small
 */
int32_t fgr_debug_hex_dump_to_buffer(const void *data, size_t data_size,
                                     char *output, size_t output_size);

#ifdef __cplusplus
}
#endif
/** @}*/

#endif // _FGR_DEBUG_H_

// End of file
