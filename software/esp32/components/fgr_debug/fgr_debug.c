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

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "esp_log.h"
#include "errno.h"
#include "ctype.h"
#include "esp_mac.h"
#include "driver/gpio.h"

#include "fgr_debug.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "debug"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise debug stuff.
int32_t fgr_debug_init()
{
    esp_err_t err = ESP_OK;
#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
    // Configure our debug LED
      err = gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);
      if (err == ESP_OK) {
          err = gpio_set_direction(CONFIG_FGR_DEBUG_LED_PIN, GPIO_MODE_OUTPUT);
          if (err == ESP_OK) {
              // Flash it so that we know it can be active
              fgr_debug_flash_led(FGR_DEBUG_LED_SHORT_MS);
          }
      }
#endif
    return (int32_t) -err;
}

// Flash the debug LED.
void fgr_debug_flash_led(int32_t duration_ms)
{
#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
    gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 0);
    vTaskDelay(pdMS_TO_TICKS(duration_ms));
    gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);
#endif
}

// Print out our MAC address.
void fgr_debug_print_mac_address()
{
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        ESP_LOGI(TAG, "MAC address %02X:%02X:%02X:%02X:%02X:%02X", mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
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

    if (output_size == 0) return -1;

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

