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

#ifndef _FGR_RCWL9160A_H_
#define _FGR_RCWL9160A_H_

/** @file
 * @brief API to read distance from an RCWL-9610A chip over UART,
 * a node of the front garden railway.
 */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** Initialise the interface to the RCWL-9610A.
 *
 * @param uart      the UART number to use (e.g. UART_NUM_1).
 * @param pin_txd   the GPIO number for the transmit data pin, e.g. 7.
 * @param pin_rxd   the GPIO number for the receive data pin, e.g. 6.
 * @return          ESP_OK on success, else a negative value from esp_err_t.
 */
int32_t fgr_rcwl9610a_init(int32_t uart, int32_t pin_txd, int32_t pin_rxd);

/** Deinitialise the interface to the RCWL-9610A.
 */
void fgr_rcwl9610a_deinit();

/** Make a distance reading.
 *
 * @return the distance in millimetres, else a negative value from esp_err_t.
 */
int32_t fgr_rcwl9610a_read();

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_RCWL9160A_H_

// End of file
