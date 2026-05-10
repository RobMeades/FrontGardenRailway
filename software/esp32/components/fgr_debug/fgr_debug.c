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
#include "esp_log.h"
#include "errno.h"
#include "ctype.h"
#include "esp_mac.h"
#include "driver/gpio.h"
#include "driver/spi_common.h"
#include "driver/spi_master.h"

#include "fgr_debug.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "debug"

// The WS2812 RGB LED used for the CONFIG_FGR_DEBUG_LED_SPI_NUM case,
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
// Meaning of bits is 8 bits green then 8 bits red then 8 bits blue
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

// The number of SPI bits per WS2812 GRB transaction: 3
// WS2812 bytes plus the end of group low time
#define SPI_BITS_PER_WS2812_TRANSACTION ((SPI_BITS_PER_WS2812_BYTE * 3) + WS2812_END_OF_GROUP_SPI_BITS_LOW)

// The buffer size (in bytes) to hold a WS2812 GRB transaction
#define SPI_TRANSACTION_BUFFER_LENGTH_BYTES ((SPI_BITS_PER_WS2812_TRANSACTION / 8) + 1)

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// SPI device handle
static spi_device_handle_t g_spi = NULL;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

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
        // Encode the three bytes, order GRB
        ws2812_encode_byte(colour->green, &buffer_ptr, &length);
        ws2812_encode_byte(colour->red, &buffer_ptr, &length);
        ws2812_encode_byte(colour->blue, &buffer_ptr, &length);
        encoded_length_bits = SPI_BITS_PER_WS2812_TRANSACTION;
    }

    // Return the total bit length (including reset bits)
    return encoded_length_bits;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise debug stuff.
int32_t fgr_debug_init()
{
    esp_err_t err = ESP_OK;
#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally
   // Use SPI to clock out the 24 bits of RGB to s WS2812 LED.
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
        err = spi_bus_add_device(CONFIG_FGR_DEBUG_LED_SPI_NUM, &dev_cfg, &g_spi);
        if (err == ESP_OK) {
            // Flash it so that we know it can be active
            fgr_debug_flash_led(FGR_DEBUG_LED_LONG_MS, FGR_DEBUG_LED_COLOUR_BOOT);
        } else {
            spi_bus_free(CONFIG_FGR_DEBUG_LED_SPI_NUM);
            g_spi = NULL; // Just in case
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
            fgr_debug_flash_led(FGR_DEBUG_LED_LONG_MS, FGR_DEBUG_LED_COLOUR_BOOT);
        } else {
            ESP_LOGE(TAG, "gpio_set_direction() on pin %d failed (%s)!",
                     CONFIG_FGR_DEBUG_LED_PIN, esp_err_to_name(err));
        }
    } else {
        ESP_LOGE(TAG, "gpio_set_level() on pin %d failed (%s)!",
                 CONFIG_FGR_DEBUG_LED_PIN, esp_err_to_name(err));
    }
#  endif
#endif
    return (int32_t) -err;
}

// Flash the debug LED.
void fgr_debug_flash_led(int32_t duration_ms, fgr_debug_colour_t colour)
{
#if defined(CONFIG_FGR_DEBUG_LED_PIN) && (CONFIG_FGR_DEBUG_LED_PIN >= 0)
#  if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally
    // WS2812 RGB LED driven via SPI
    if (g_spi) {
        uint8_t *buffer = (uint8_t *) heap_caps_malloc(SPI_TRANSACTION_BUFFER_LENGTH_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
        if (buffer) {
            fgr_debug_colour_t off = {0, 0, 0};
            spi_transaction_t transaction = {0};

            transaction.length = ws2812_spi_transaction(&colour, buffer, SPI_TRANSACTION_BUFFER_LENGTH_BYTES);
            transaction.tx_buffer = buffer;
            esp_err_t err = spi_device_transmit(g_spi, &transaction);
            if (err == ESP_OK) {
                    vTaskDelay(pdMS_TO_TICKS(duration_ms));
                    transaction.length = ws2812_spi_transaction(&off, buffer, SPI_TRANSACTION_BUFFER_LENGTH_BYTES);
                    spi_device_transmit(g_spi, &transaction);
            } else {
                ESP_LOGE(TAG, "spi_device_transmit() failed (%s)!", esp_err_to_name(err));
            }

            // Free
            heap_caps_free(buffer);
        } else {
            ESP_LOGE(TAG, "heap_caps_malloc() for %d byte(s) failed!", SPI_TRANSACTION_BUFFER_LENGTH_BYTES);
        }
    }
#  else
    // Single colour LED
    gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 0);
    vTaskDelay(pdMS_TO_TICKS(duration_ms));
    gpio_set_level(CONFIG_FGR_DEBUG_LED_PIN, 1);
#  endif
#endif
}

// Initialise debug stuff.
void fgr_debug_deinit()
{
#if defined(CONFIG_FGR_DEBUG_LED_SPI_NUM) && (CONFIG_FGR_DEBUG_LED_SPI_NUM > 1) // SPIs 0 and 1 are used internally
    if (g_spi) {
        spi_bus_free(CONFIG_FGR_DEBUG_LED_SPI_NUM);
        g_spi = NULL;
    }
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

