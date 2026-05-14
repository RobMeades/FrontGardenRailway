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

// Required for fgr_msg_t.
#include "../../../../../protocol/fgr_protocol.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_DEBUG_LED_SHORT_MS
// Standard short duration for an LED lash.
#  define FGR_DEBUG_LED_SHORT_MS 250
#endif

#ifndef FGR_DEBUG_LED_LONG_MS
// Standard short duration for a long LED flash.
#  define FGR_DEBUG_LED_LONG_MS 1000
#endif

#ifndef FGR_DEBUG_LED_INTENSITY_LOW
// How bright to shine the LED for low intensity; ignored
// for a single colour LED: these LEDS are very bright!
#  define FGR_DEBUG_LED_INTENSITY_LOW 16
#endif

#ifndef FGR_DEBUG_LED_INTENSITY_HIGH
// How bright to shine the LED for high intensity; ignored
// for a single colour LED.
#  define FGR_DEBUG_LED_INTENSITY_HIGH 32
#endif

#define FGR_DEBUG_LED_COLOUR_NONE ((fgr_debug_colour_t) {0, 0, 0})

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

#ifndef FGR_DEBUG_LED_COLOUR_NEEDS_CFG
// Standardised colour, primarily for breathe, when waiting for configuration.
#  define FGR_DEBUG_LED_COLOUR_NEEDS_CFG FGR_DEBUG_LED_COLOUR_CYAN
#endif

#ifndef FGR_DEBUG_LED_COLOUR_STOPPED
// Standardised colour, primarily for breathe, when stopped.
#  define FGR_DEBUG_LED_COLOUR_STOPPED FGR_DEBUG_LED_COLOUR_MAGENTA
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
#  define FGR_DEBUG_LED_COLOUR_NOTIFY FGR_DEBUG_LED_COLOUR_YELLOW
#endif

#ifndef FGR_DEBUG_LED_COLOUR_MSG_SENT
// Standardised message sent colour.
#  define FGR_DEBUG_LED_COLOUR_MSG_SENT FGR_DEBUG_LED_COLOUR_BLUE
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

/** Function to call to obtain the state of a node.
 *
 * @param param  cb_param as passed to fgr_debug_init().
 * @return       the state of the node.
 */
typedef fgr_state_t (*fgr_debug_state_cb_t)(void *param);

/* ----------------------------------------------------------------
 * FUNCTIONS: INITIALISE/DEINITIALISE
 * -------------------------------------------------------------- */

/** Initialise debug.
 *
 * Note: this will create a semaphore that is never destroyed.
 *
 * @param cb         a pointer to a function that will return
 *                   the state of the node.  If using a
 *                   WS2812 debug LED
 *                   (i.e. CONFIG_FGR_DEBUG_LED_SPI_NUM is defined)
 *                   this will be called to determine the
 *                   breathe colour.  Note that this necessarily
 *                   entails making assumptions about what a
 *                   given state means: for instance, if you
 *                   have node-specific states or you have states
 *                   that indicate that everything is OK _after_
 *                   the ones that indicate otherwise (like
 *                   FGR_STATE_GENERIC_FAILED) then you probably
 *                   want to pass NULL here and instead update
 *                   the breathe colour directly yourself using
 *                   fgr_debug_led_breathe_set().  Ignored if
 *                   CONFIG_FGR_DEBUG_LED_SPI_NUM is not
 *                   defined.
 * @param cb_param   parameter that will be passed to cb()
 *                   when it is called; may be NULL.
 * @return           ESP_OK on success, else a negative value from
 *                   esp_err_t.
 */
int32_t fgr_debug_init(fgr_debug_state_cb_t cb, void *cb_param);

/** Deinitialise debug.  This will also free any remaining tasks,
 * and do so in a coooperative way, waiting for any task callbacks
 * to return, no crowbars.
 */
void fgr_debug_deinit();

/* ----------------------------------------------------------------
 * FUNCTIONS: LED RELATED
 * -------------------------------------------------------------- */

/** Flash the debug LED.
 *
 * @param duration_ms how long to flash the LED for (e.g.
 *                    FGR_DEBUG_LED_SHORT_MS or FGR_DEBUG_LED_LONG_MS).
 * @param colour      the LED colour, ignored if
 *                    CONFIG_FGR_DEBUG_LED_IS_WS2812 is not defined.
 */
void fgr_debug_led_flash(int32_t duration_ms, fgr_debug_colour_t colour);

/** Turn the LED "breathe" effect off.  If fgr_nvs_init() or
 * fgs_ota_init() have been called then the setting will persist
 * across boot cycles.  The breathe effect only runs if you are using
 * a WS2812 debug LED (i.e. CONFIG_FGR_DEBUG_LED_SPI_NUM is defined).
 */
void fgr_debug_led_breathe_off(void);

/** Turn the LED "breathe" effect on (i.e. continue to operate
 * as it did before fgr_debug_led_breathe_off()).  If fgr_nvs_init()
 * or fgs_ota_init() have been called then the setting will persist
 * across boot cycles.  The breathe effect only runs if you are using
 * a WS2812 debug LED (i.e. CONFIG_FGR_DEBUG_LED_SPI_NUM is defined).
 */
void fgr_debug_led_breathe_on(void);

/** Turn all debug LEDs off.  If fgr_nvs_init() or fgs_ota_init()
 * have been called then the setting will persist across boot cycles.
 */
void fgr_debug_led_off(void);

/** Turn all debug LEDs on (i.e. continue to operate as they
 * did before fgr_debug_led_off()).  If fgr_nvs_init() or
 * fgs_ota_init() have been called then the setting will persist
 * across boot cycles.
 */
void fgr_debug_led_on(void);

/** Set the LED "breathing" manually: you would not normally call
 * this, just let the debug function operate the breathe effect
 * based on the state callback passed to fgr_debug_init().  To
 * return to normal operation after manually setting a breathe
 * effect, pass in all zeros for the colours.  The breathe
 * effect only runs if you are using a WS2812 debug LED
 * (i.e. CONFIG_FGR_DEBUG_LED_SPI_NUM is defined).
 *
 * Note: LED breathing is by default on but this will do nothing
 * if fgr_debug_led_breathe_off() was previously called, or if
 * non-volatile storage is in play and fgr_debug_led_breathe_off()
 * was called in a previous boot.
 *
 * @param colour the LED colour.
 */
void fgr_debug_led_breathe_set(fgr_debug_colour_t colour);

/* ----------------------------------------------------------------
 * FUNCTIONS: MISC
 * -------------------------------------------------------------- */

/** A message receive callback that will handle
 * the FGR_REQ_CNF_DEBUG_* messages: add this to your
 * application's message receive chain (before
 * your own handlers so that it is below them) with:
 *
 * fgr_msg_receive_handler_add(0, fgr_debug_msg_receive_cb, NULL);
 *
 * ...and this code will deal with them for you.
 *
 * IMPORTANT: for this to work your application must
 * set up a message send queue (i.e. must have called
 * fgr_msg_send_queue_init()).
 *
 * @param msg    a pointer to the received message.
 * @param param  cb_param as passed to fgr_msg_receive_handler_add().
 * @return       true if the message is handled, false if it
 *               can be passed to subsequent handlers.
 */
bool fgr_debug_msg_receive_cb(fgr_msg_t *msg, void *param);

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

/** @} */

#endif // _FGR_DEBUG_H_

// End of file
