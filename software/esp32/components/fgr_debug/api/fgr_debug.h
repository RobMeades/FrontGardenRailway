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
  * @brief The ping API for the front garden railway.
  */
 
#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

#ifndef FGR_DEBUG_LED_SHORT_MS
// Standard short duration for an LED lash
#  define FGR_DEBUG_LED_SHORT_MS 50
#endif

#ifndef FGR_DEBUG_LED_LONG_MS
// Standard short duration for a long LED lash
#  define FGR_DEBUG_LED_LONG_MS 1000
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

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
 */
void fgr_debug_flash_led(int32_t duration_ms);

/** Print out our MAC address if possible.
 */
void fgr_debug_print_max_address();

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
