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
 * @brief Retained RAM functions for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include "esp_err.h"
#include "esp_rom_crc.h"

#include "fgr_rram.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Logging prefix
#define TAG "rram"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Set a retained RAM variable.
int32_t fgr_rram_set(const void *variable, void *rram_variable,
                     size_t rram_variable_size)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (variable && rram_variable) {
        err = ESP_OK;
        size_t payload_size = rram_variable_size - sizeof(uint32_t);
        size_t total_words = payload_size / sizeof(uint32_t);

        // Compute the CRC over the stable DRAM shadow payload before touching the RTC bus
        uint32_t crc = esp_rom_crc32_le(0xFFFFFFFF, (const uint8_t *) variable, payload_size);

        // Cast the pointers to strict 32-bit integer arrays
        const uint32_t *src_words = (const uint32_t *) variable;
        uint32_t *dst_words = (uint32_t *) rram_variable;

        // Force a strict, un-widened 32-bit hardware bus copy loop
        // Using volatile here blocks the compiler from converting this into
        // 64-bit/128-bit instructions
        volatile uint32_t *volatile_dst_words = (volatile uint32_t *) dst_words;

        for (size_t x = 0; x < total_words; x++) {
            volatile_dst_words[x] = src_words[x];
        }

        volatile_dst_words[total_words] = crc;
    }

    return err;
}

// Get a retained RAM variable into a normal variable.
int32_t fgr_rram_get(const void *rram_variable, void *variable,
                     size_t rram_variable_size)
{
    int32_t err = -ESP_ERR_INVALID_ARG;

    if (variable && rram_variable) {
        err = -ESP_ERR_INVALID_CRC;
        size_t payload_size = rram_variable_size - sizeof(uint32_t);
        size_t total_words = payload_size / sizeof(uint32_t);

        // Mark the source as volatile to force strict 32-bit reads from the RTC bus
        const volatile uint32_t *volatile_src_words = (const volatile uint32_t *) rram_variable;
        uint32_t *dst_words = (uint32_t *) variable;

        uint32_t crc = 0xFFFFFFFF;
        for (size_t x = 0; x < total_words; x++) {
            uint32_t word = volatile_src_words[x]; // Strict 32-bit bus read instruction
            dst_words[x] = word;                   // Write to variable

            // Feed the 4 bytes of this 32-bit word into the CRC calculation
            crc = esp_rom_crc32_le(crc, (const uint8_t *) &word, sizeof(uint32_t));
        }

        if (crc == volatile_src_words[total_words]) {
            err = ESP_OK;
        } else {
            for (size_t x = 0; x < total_words; x++) {
                dst_words[x] = 0;
            }
        }
    }

    return err;
}

// Clear a retained RAM variable safely.
void fgr_rram_clear(void *rram_variable, size_t rram_variable_size)
{
    if (rram_variable) {
        size_t payload_size = rram_variable_size - sizeof(uint32_t);
        size_t total_words = payload_size / sizeof(uint32_t);

        // A block of zeroes has a predictable, non-zero, CRC
        uint32_t crc = 0xFFFFFFFF;
        const uint32_t zero_word = 0;

        uint32_t *dst_words = (uint32_t *) rram_variable;

        // The 'volatile' qualifier forces GCC to emit individual S32I (Store 32-bit)
        // assembly instructions, completely blocking the Loop Distribution optimization pass
        volatile uint32_t *volatile_dst_words = (volatile uint32_t *) dst_words;

        for (size_t x = 0; x < total_words; x++) {
            volatile_dst_words[x] = 0;

            // Calculate the valid CRC for an all-zero payload inline
            crc = esp_rom_crc32_le(crc, (const uint8_t *) &zero_word, sizeof(uint32_t));
        }

        // Latch the correct "all-zero payload" CRC into the final slot
        volatile_dst_words[total_words] = crc;
    }
}

// End of file
