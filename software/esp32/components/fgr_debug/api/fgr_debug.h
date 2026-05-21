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

#ifndef FGR_DEBUG_BACKTRACE_DEPTH_MAX
// The maximim depth of a backtrace.
#  define FGR_DEBUG_BACKTRACE_DEPTH_MAX 32
#endif

#ifndef FGR_DEBUG_BACKTRACE_FORMAT_STRING
// The printf() format string for an item in a backtrace.
#  define FGR_DEBUG_BACKTRACE_FORMAT_STRING "0x%08x "
#endif

#ifndef FGR_DEBUG_BACKTRACE_NUMBER_LENGTH
// The length of buffer required for the format string
// FGR_DEBUG_BACKTRACE_FORMAT_STRING **WITHOUT** a terminator.
#  define FGR_DEBUG_BACKTRACE_NUMBER_LENGTH 11
#endif

#ifndef FGR_DEBUG_TASK_NAME_MAX_LENGTH
// The maximum length of a task name (for stack overflows) including
// room for a null terminator.
#  define FGR_DEBUG_TASK_NAME_MAX_LENGTH (16 + 1)
#endif

// The length of buffer required to encode the maximum length
// backtrace, including room for a null terminator, enough space
// for FGR_DEBUG_BACKTRACE_BUFFER_NUMBER_LENGTH characters N times
// plus the terminator.
#define FGR_DEBUG_BACKTRACE_BUFFER_LENGTH ((FGR_DEBUG_BACKTRACE_DEPTH_MAX * FGR_DEBUG_BACKTRACE_NUMBER_LENGTH) + 1)

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

/** Initialise debug.  Needs a task so fgr_util_init() must
 * have been called first.  It is always safe to call this at any
 * time: if already initialised it will do nothing and return success.
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
 * to return, no crowbars.  It is always safe to call this at any time.
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
 * FUNCTIONS: PANIC
 * -------------------------------------------------------------- */

/** Function to obtain any backtrace captured after a panic.
 * For this to work, the main project CMakeLists.txt file
 * must have:
 *
 * idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=esp_panic_handler" APPEND)
 *
 * ...which will wrap calls to esp_panic_handler().
 *
 * You might call this function on boot and log the result so that,
 * if you have the ELF file, the function call tree leading
 * to the panic can be printed using the Espressif ESP-IDF tools:
 *
 * xtensa-esp-elf-addr2line -pfiaC -e my_binary.elf <backtrace in hex>
 *
 * e.g.:
 *
 * xtensa-esp-elf-addr2line -pfiaC -e test.elf 0x400D1234 0x400D5678 ...
 *
 *
 * See also fgr_debug_panic_str_get() ,fgr_debug_panic_str_get()
 * and fgr_debug_panic_log(.)
 *
 * @param backtrace  a pointer to storage for
 *                   FGR_DEBUG_BACKTRACE_DEPTH_MAX uint32_t values
 *                   that are the backtrace; may be NULL, in which
 *                   case the backtrace is retained and you might
 *                   use the return value to size your storage
 *                   before calling this function again.  If non-NULL
 *                   the backtrace storage is emptied on return.
 * @return           if there have been one or more panics since
 *                   power-on, the number of uint32_t values
 *                   that would be populated in backtrace if it
 *                   were non-NULL, ESP_OK if there have been no
 *                   panics.
 */
int32_t fgr_debug_panic_get(uint32_t *backtrace);

/** As fgr_debug_panic_get() but populates a buffer with
 * a string that can be passed straight to xtensa-esp-elf-addr2line,
 * e.g. "0x400D1234 0x400D5678..."
 *
 * See also fgr_debug_panic_str_get();
 *
 * @param buffer a pointer to storage for up to
 *               FGR_DEBUG_BACKTRACE_BUFFER_LENGTH that will be
 *               populated with the backtrace string; may be NULL,
 *               in which case the backtrace is retained and you
 *               might use the return value to size your storage
 *               before calling this function again.  If non-NULL
 *               the backtrace storage is emptied on return.
 * @return       if there have been one or more panics since
 *               power-on, the number of characters that would be
 *               populated in buffer (i.e. what strlen() would
 *               return) if it were not NULL, ESP_OK if there have
 *               been no panics.
 */
int32_t fgr_debug_panic_str_get(char *buffer);

/** As fgr_debug_panic_str_get() but instead of returning a
 * string, logs the backtrace string (if present) to an ESP_LOGx()
 * macro with the given ESP-IDF log level.
 *
 * @param tag    the tag to apply to the log message; may be NULL
 *               in which case whatever is the default tag for
 *               debug messages will be employed.
 * @param prefix a prefix to put in front of the backtrace string;
 *               may be NULL.
 * @param level  the log level to log the string as.
 * @return       1 if there was a panic, ESP_OK if not, else a
 *               negative error code from esp_err_t.
 */
int32_t fgr_debug_panic_log(const char *tag, const char *prefix,
                            esp_log_level_t level);

/* ----------------------------------------------------------------
 * FUNCTIONS: STACK OVERFLOW
 * -------------------------------------------------------------- */

/** Get the name of a task that had a stack overflow; you might call
 * this at boot to see if the boot was actually a reboot resulting
 * from a stack overflow.
 *
 * @param buffer a pointer to storage for up to
 *               FGR_DEBUG_TASK_NAME_MAX_LENGTH characters that will
 *               be populated with the task name; may be NULL,
 *               in which case the task name is retained and you
 *               might use the return value to size your storage
 *               before calling this function again.  If non-NULL
 *               the task name is emptied on return.
 * @return       if a stack overflow occurred, the number
 *               characters that would be populated in buffer
 *               (i.e. what strlen() would return) if it were
 *               non-NULL, ESP_OK if there was no stack overflow.
 */
int32_t fgr_debug_stack_overflow_get(char *buffer);

/** As fgr_debug_stack_overflow_get() but instead of returning
 * the task name string, logs the task name string (if present) to
 * an ESP_LOGx() macro with the given ESP-IDF log level.
 *
 * @param tag    the tag to apply to the log message; may be NULL
 *               in which case whatever is the default tag for
 *               debug messages will be employed.
 * @param prefix a prefix to put in front of the backtrace string;
 *               may be NULL.
 * @param level  the log level to log the string as.
 * @return       1 if there was a stack overflow, ESP_OK if not,
 *               else a negative error code from esp_err_t.
 */
int32_t fgr_debug_stack_overflow_log(const char *tag, const char *prefix,
                                     esp_log_level_t level);

/* ----------------------------------------------------------------
 * FUNCTIONS: CORE DUMP
 * -------------------------------------------------------------- */

/** Send a core dump to logging; you might call this at boot to see
 * if there is a core dump stored to flash.  The core dump will be sent
 * in ESP_LOGx() messages, base64 encoded.
 *
 * For this to work, you must have a flash partition of at least
 * 64 kbytes dedicated for core dumps, e.g. like this:
 *
 * coredump,   data, coredump,0x3E0000, 64K
 *
 * ...and you must have set CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y
 * and likely CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF=y in your sdkconfig
 * file.  With this configuration, core dumps are automatically sent
 * by ESP-IDF when a crash occurs.
 *
 * @param tag    the tag to apply to the log message; may be NULL
 *               in which case whatever is the default tag for
 *               debug messages will be employed.
 * @param level  the log level to log the string as.
 * @return       1 if a core dump as present, ESP_OK if not,
 *               else a negative error code from esp_err_t.
 */
int32_t fgr_debug_core_dump_get(const char *tag, esp_log_level_t level);

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
