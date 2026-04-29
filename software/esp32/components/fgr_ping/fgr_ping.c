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
 * @brief Implementation of the ping API for a node of the front garden railway.
 */

#include <string.h>
#include <inttypes.h>
#include "esp_log.h"
#include "errno.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "ping/ping_sock.h"

#include "fgr_ping.h"

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

 // Logging prefix
 #define TAG "ping"

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * VARIABLES
 * -------------------------------------------------------------- */

// Place to hook the callback to be called on a ping loss.
static fgr_ping_loss_cb_t g_ping_loss_cb = NULL;

// Place to hook the user parameter to be passed to g_ping_loss_cb.
static void *g_ping_loss_cb_arg = NULL;

/* ----------------------------------------------------------------
 * STATIC FUNCTIONS
 * -------------------------------------------------------------- */

// Call back to ping session called on receipt of a successful ping response.
static void cmd_ping_on_ping_success(esp_ping_handle_t hdl, void *args)
{
    uint8_t ttl;
    uint16_t seqno;
    uint32_t elapsed_time, recv_len;
    ip_addr_t target_addr;
    esp_ping_get_profile(hdl, ESP_PING_PROF_SEQNO, &seqno, sizeof(seqno));
    esp_ping_get_profile(hdl, ESP_PING_PROF_TTL, &ttl, sizeof(ttl));
    esp_ping_get_profile(hdl, ESP_PING_PROF_IPADDR, &target_addr, sizeof(target_addr));
    esp_ping_get_profile(hdl, ESP_PING_PROF_SIZE, &recv_len, sizeof(recv_len));
    esp_ping_get_profile(hdl, ESP_PING_PROF_TIMEGAP, &elapsed_time, sizeof(elapsed_time));
    ESP_LOGI(TAG, "%" PRIu32 " byte(s) from %s icmp_seq=%" PRIu16 " ttl=%" PRIu16 " time=%" PRIu32 " ms.",
        recv_len, ipaddr_ntoa((ip_addr_t*)&target_addr), seqno, ttl, elapsed_time);
}

// Call back to ping session called on a ping timeout.
static void cmd_ping_on_ping_timeout(esp_ping_handle_t hdl, void *args)
{
    uint16_t seqno;
    ip_addr_t target_addr;
    esp_ping_get_profile(hdl, ESP_PING_PROF_SEQNO, &seqno, sizeof(seqno));
    esp_ping_get_profile(hdl, ESP_PING_PROF_IPADDR, &target_addr, sizeof(target_addr));
    ESP_LOGI(TAG, "From %s icmp_seq=%d timeout.",ipaddr_ntoa((ip_addr_t*)&target_addr), seqno);
    if (g_ping_loss_cb) {
        g_ping_loss_cb(g_ping_loss_cb_arg);
    }
}

// Call back to ping session called at end.
static void cmd_ping_on_ping_end(esp_ping_handle_t hdl, void *args)
{
    ip_addr_t target_addr;
    uint32_t transmitted;
    uint32_t received;
    uint32_t total_time_ms;
    uint32_t loss;

    esp_ping_get_profile(hdl, ESP_PING_PROF_REQUEST, &transmitted, sizeof(transmitted));
    esp_ping_get_profile(hdl, ESP_PING_PROF_REPLY, &received, sizeof(received));
    esp_ping_get_profile(hdl, ESP_PING_PROF_IPADDR, &target_addr, sizeof(target_addr));
    esp_ping_get_profile(hdl, ESP_PING_PROF_DURATION, &total_time_ms, sizeof(total_time_ms));

    if (transmitted > 0) {
        loss = (uint32_t)((1 - ((float)received) / transmitted) * 100);
    } else {
        loss = 0;
    }
    ESP_LOGI(TAG, "%" PRIu32 " packets transmitted, %" PRIu32 " received, %" PRIu32 "%% packet loss, average time %" PRIu32 " ms.",
                  transmitted, received, loss, total_time_ms / received);
    // delete the ping sessions, so that we clean up all resources and can create a new ping session
    // we don't have to call delete function in the callback, instead we can call delete function from other tasks
    esp_ping_delete_session(hdl);
    g_ping_loss_cb = NULL;
    g_ping_loss_cb_arg = NULL;
}

/* ----------------------------------------------------------------
 * PUBLIC FUNCTIONS
 * -------------------------------------------------------------- */

// Ping the given host name, which should be a null-terminated string.
int32_t fgr_ping_start(const char *hostname, int32_t count,
                       int32_t interval_ms, int32_t timeout_ms,
                       int32_t data_size,
                       fgr_ping_loss_cb_t ping_loss_cb,
                       void *ping_loss_cb_arg)
{
    esp_err_t err = ESP_ERR_INVALID_ARG;
    esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
    struct addrinfo hint;
    struct addrinfo *res = NULL;
    memset(&hint, 0, sizeof(hint));

    /* convert ip4 string or hostname to ip4 address */
    ip_addr_t target_addr;
    memset(&target_addr, 0, sizeof(target_addr));

    if (getaddrinfo(hostname, NULL, &hint, &res) != 0) {
        ESP_LOGI(TAG, "Unknown host: \"%s\".", hostname);
    } else {
        if (res->ai_family == AF_INET) {
            struct in_addr addr4 = ((struct sockaddr_in *) (res->ai_addr))->sin_addr;
            inet_addr_to_ip4addr(ip_2_ip4(&target_addr), &addr4);
        }
        config.target_addr = target_addr;
        if (count >= 0) {
            config.count = count;
        }
        if (interval_ms >= 0) {
            config.interval_ms = interval_ms;
        }
        if (timeout_ms >= 0) {
            config.timeout_ms = timeout_ms;
        }
        if (data_size >= 0) {
            config.data_size = data_size;
        }
        /* set callback functions */
        esp_ping_callbacks_t cbs = {
            .cb_args = NULL,
            .on_ping_success = cmd_ping_on_ping_success,
            .on_ping_timeout = cmd_ping_on_ping_timeout,
            .on_ping_end = cmd_ping_on_ping_end
        };
        esp_ping_handle_t ping;
        err = esp_ping_new_session(&config, &cbs, &ping);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to initialise ping session.");
        } else {
            err = esp_ping_start(ping);
            if (err == ESP_OK) {
                // Leave this late so that the entries
                // are only configured on success.
                g_ping_loss_cb = ping_loss_cb;
                g_ping_loss_cb_arg = ping_loss_cb_arg;
            } else {
                ESP_LOGE(TAG, "Failed to start ping to \"%s\".", hostname);
            }
        }
    }
    freeaddrinfo(res);

    // Returns ESP_OK or negative error code from esp_err_t
    return (int32_t) -err;
}

// End of file

