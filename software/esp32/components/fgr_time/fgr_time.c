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
 * @brief Time functions for a node of the front garden railway.
 */

// Ensure we are compiling with maximum debug, can then be trimmed
// at run-time by fgr_log
#define LOG_LOCAL_LEVEL ESP_LOG_DEBUG

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"

#include "fgr_util.h"
#include "fgr_time.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "time"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// Structure to store the power on time in retained RAM.
typedef struct {
    int32_t magic;
    time_t utc;
} power_on_time_t;

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Storage for the power-on time in retained RAM.
RTC_NOINIT_ATTR power_on_time_t g_power_on_time;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Callback for when time has been synchronised.
static void time_sync_cb(struct timeval *tv)
{
    char buffer[64];
    struct tm local;

    if (g_power_on_time.magic != FGR_UTIL_RETAINED_RAM_MAGIC_MARKER) {
        g_power_on_time.utc = tv->tv_sec - fgr_time_since_boot();
        g_power_on_time.magic = FGR_UTIL_RETAINED_RAM_MAGIC_MARKER;
    }

    localtime_r(&tv->tv_sec, &local);
    strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &local);
    ESP_LOGI(TAG, "NTP time synchronized: %s", buffer);
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Initialise time.
int32_t fgr_time_init(const char *ntp_server_ip_address,
                      const char *timezone,
                      size_t ntp_sync_interval_seconds)
{
    int32_t err = ESP_OK;

    if (timezone) {
        setenv("TZ", timezone, 1);
        tzset();
    }

    if (ntp_server_ip_address) {

        // Must be called before esp_netif_sntp_init()
        sntp_set_sync_interval(ntp_sync_interval_seconds * 1000);

        esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG(ntp_server_ip_address);
        cfg.sync_cb = time_sync_cb;
        err = esp_netif_sntp_init(&cfg);
    }

    return err;
}

// Deinitialise time.
void fgr_time_deinit()
{
    esp_netif_sntp_deinit();
}

// Get the time since boot in seconds.
time_t fgr_time_since_boot()
{
    return (time_t) ((esp_timer_get_time()) / 1000000ULL);
}

// Get the time since power-on in seconds.
time_t fgr_time_since_power_on()
{
    time_t time = -ESP_ERR_NOT_FOUND;
    time_t utc = fgr_time_utc();

    if ((g_power_on_time.magic == FGR_UTIL_RETAINED_RAM_MAGIC_MARKER) && (utc >= 0)) {
        time = utc - g_power_on_time.utc;
    }

    return time;
}

// Get the UTC time.
time_t fgr_time_utc()
{
    time_t now = 0;
    time(&now);
    return (g_power_on_time.magic == FGR_UTIL_RETAINED_RAM_MAGIC_MARKER) ? now : -ESP_ERR_NOT_FOUND;
}

// Get the local time.
time_t fgr_time_local()
{
    time_t local = -ESP_ERR_NOT_FOUND;
    time_t utc = fgr_time_utc();

    if (utc >= 0) {
        struct tm local_tm = {0};
        localtime_r(&utc, &local_tm);
        local = mktime(&local_tm);
    }

    return local;
}

// End of file

