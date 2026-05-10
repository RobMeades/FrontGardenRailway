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
 * @brief Implementation of the NVS API for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "errno.h"

#include "fgr_nvs.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "nvs"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Only thing we need to track is whether we've been initialised.
static bool g_initialised = false;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise NVS.
int32_t fgr_nvs_init()
{
    esp_err_t err = ESP_OK;

    if (!g_initialised) {
        // Initialize NVS
        err = nvs_flash_init();
        if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
            // OTA app partition table has a smaller NVS partition size than the non-OTA
            // partition table. This size mismatch may cause NVS initialization to fail.
            // If this happens, we erase NVS partition and initialize NVS again.
            esp_err_t erase_err = nvs_flash_erase();
            if (erase_err == ESP_OK) {
                err = nvs_flash_init();
            } else {
                ESP_LOGE(TAG, "Failed to erase NVS: %s.", esp_err_to_name(erase_err));
            }
        }

        if (err == ESP_OK) {
            g_initialised = true;
        } else {
            ESP_LOGE(TAG, "Failed to initialize NVS: %s.", esp_err_to_name(err));
        }
    }

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// End of file

