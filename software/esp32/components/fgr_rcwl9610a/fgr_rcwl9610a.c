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
 * @brief Implementation of an RCWL-9610A driver that reads a distance
 * value in millimetres for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_log.h"
#include "driver/uart.h"

#include "fgr_rcwl9610a.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix.
 #define TAG "rcwl9610a"

// UART buffer size (has to be at least as big as UART_HW_FIFO).
#define UART_RX_BUFFER_SIZE 256

// Expected UART read length (3 bytes for a length reading).
#define UART_RX_READ_SIZE 3

// UART baud rate (must be 9600).
#define UART_BAUD_RATE 9600

// The single byte command that reads the distance from an RCWL-9610A.
#define RCWL9610A_COMMAND_READ_DISTANCE 0xa0

// How long to wait for an RCWL-9610A to make a measurement, in milliseconds.
#define RCWL9610A_MEASUREMENT_WAIT_MS 120

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// The UART we are using, -1 if fgr_rcwl9610a_init() has not been called.
static int32_t g_uart = -1;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Clean-up on error or completion.
static void clean_up()
{
    if (g_uart >= 0) {
        uart_driver_delete((uart_port_t) g_uart);
        g_uart = -1;
    }
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise the interface to the RCWL-9610A.
int32_t fgr_rcwl9610a_init(int32_t uart, int32_t pin_txd, int32_t pin_rxd)
{
    esp_err_t err = ESP_OK;

    if (g_uart < 0) {
        // UART configuration
        uart_config_t uart_config = {
            .data_bits = UART_DATA_8_BITS,
            .parity = UART_PARITY_DISABLE,
            .stop_bits = UART_STOP_BITS_1,
            .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
            .source_clk = UART_SCLK_DEFAULT,
            .baud_rate = UART_BAUD_RATE
        };

        ESP_LOGI(TAG, "Installing RCWL-9610A driver on UART %d, TXD pin %d,"
                " RXD pin %d, baud rate %d.", uart, pin_txd, pin_rxd,
                UART_BAUD_RATE);
        // Configure the UART that talks to the RCWL-9610A
        err = uart_driver_install(uart, UART_RX_BUFFER_SIZE, 0, 0, NULL, 0);
        if (err == ESP_OK) {
            g_uart = uart;
            err = uart_param_config(uart, &uart_config);
            if (err == ESP_OK) {
                err = uart_set_pin(uart, pin_txd, pin_rxd, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
                if (err != ESP_OK) {
                    ESP_LOGE(TAG, "uart_set_pin() failed (%s)!", esp_err_to_name(err));
                }
            } else {
                ESP_LOGE(TAG, "uart_param_config() failed (%s)!", esp_err_to_name(err));
            }
            if (err != ESP_OK) {
                clean_up();
            }
        } else {
            ESP_LOGE(TAG, "uart_driver_install() failed (%s)!", esp_err_to_name(err));
        }
    } else {
        ESP_LOGW(TAG, "fgr_rcwl9610a_init() called when already enabled.");
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// Deinitialise the interface to the RCWL-9610A.
void fgr_rcwl9610a_deinit()
{
    clean_up();
}

// Make a distance reading.
int32_t fgr_rcwl9610a_read()
{
    int32_t err = -ESP_ERR_INVALID_STATE;

    if (g_uart >= 0) {
        // To make a distance reading, write the command 0xA0 to the
        // RCWL-9610A and it will respond with three bytes, high,
        // medium and low, where the distance in millimetres is:
        //
        // ((BYTE_H << 16) + (BYTE_M << 8) + BYTE_L) / 1000

        uint8_t buffer[UART_RX_READ_SIZE] = {0};
        buffer[0] = RCWL9610A_COMMAND_READ_DISTANCE;
        // This function returns the number of bytes sent or -1
        err = uart_write_bytes((uart_port_t) g_uart, buffer, 1);
        if (err == 1) {
            // Wait for the chip to make a measurement
            vTaskDelay(pdMS_TO_TICKS(RCWL9610A_MEASUREMENT_WAIT_MS));
            // This function returns the number of bytes read or negative error code
            err = uart_read_bytes((uart_port_t) g_uart, buffer,
                                  sizeof(buffer), pdMS_TO_TICKS(100));
            if (err == sizeof(buffer)) {
               err = ((((int32_t) buffer[0]) << 16) + (((int32_t) buffer[1]) << 8) + buffer[2]) / 1000;
            } else {
                ESP_LOGE(TAG, "Expected to read %d byte(s) from UART %d but"
                         " uart_read_bytes() returned %d!", sizeof(buffer),
                         g_uart, err);
                err = -ESP_FAIL;
            }
        } else {
            ESP_LOGE(TAG, "Tried to write 1 byte (0x%02x) to UART %d"
                     " but uart_write_bytes() returned %d!",
                     RCWL9610A_COMMAND_READ_DISTANCE, g_uart, err);
            err = -ESP_FAIL;
        }
    }

    return err;
}

// End of file

