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

 #ifndef _FGR_PROTOCOL_H_
 #define _FGR_PROTOCOL_H_
 
/** @file
 * @brief Protocol definition for comms between an ESP32 node and
 * a controlling entity in the front garden railway.
 *
 * Note: the message elements are encoded big-endian or network
 * order on the wire (this is most readable and most common);
 * since the ESP32 and Raspberry Pi are both little-endian,
 * conversion is required for the 16-bit type (htons/ntohs) and
 * 32-bit length (htonl/ntohl) fields.
 *
 * Note: the underlying transport is assumed to be perfect and ordered.
 */
 
#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// The version of this protocol.
#define FGR_PROTOCOL_VERSION      0x01

// The maximum length of a message i.e. the length of fgr_msg_t.
#define FGR_MSG_MAX_LEN (sizeof(fgr_msg_t))

// The maximum length of a message contents field.
#define FGR_MSG_CONTENTS_MAX_LEN 256

// The maximum length of a log string (excluding the null terminator).
#define FGR_LOG_STRING_MAX_LEN   255

#if FGR_MSG_CONTENTS_MAX_LEN < (FGR_LOG_STRING_MAX_LEN) + 1
#  error "FGR_MSG_CONTENTS_MAX_LEN must be at least as large as FGR_LOG_STRING_MAX_LEN + 1"
#endif

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

// The message type; this is OR'ed with the top nibble of the request/
// confirmation/indication/response/log message. 
typedef enum {
    FGR_MSG_TYPE_NULL = 0,
    FGR_MSG_TYPE_REQ  = 1,   // Sent from a controller to a node to initiate an action
    FGR_MSG_TYPE_CNF  = 2,   // Sent from a node back to a controller confirming the action
    FGR_MSG_TYPE_IND  = 3,   // Sent from a node to a controller indicating that something has happened
    FGR_MSG_TYPE_RSP  = 4,   // Sent from a controller to a node responding to an indication
    FGR_MSG_TYPE_LOG  = 5    // A log message sent from a node to a controller
} fgr_msg_type_t;

// Request/confirmation messages; note that the top four bits must
// be zero as they will be ORed with fgr_msg_type_t.
// IF YOU ADD A MESSAGE HERE don't forget to add it to the printable list in fgr_msg.c
typedef enum {
    FGR_REQ_CNF_NULL                   = 0x0000,
    FGR_REQ_CNF_CFG                    = 0x0001, // Configure a node; the message contents are node specific
    FGR_REQ_CNF_START                  = 0x0002, // The node should start
    FGR_REQ_CNF_STOP                   = 0x0003, // The node should stop
    FGR_REQ_CNF_LOG_LEVEL              = 0x0004, // Set the log level; the REQ message contents will contain the fgr_log_level_t, encoded as a uint8_t
    FGR_REQ_CNF_LOG_START              = 0x0005, // The node should start logging
    FGR_REQ_CNF_LOG_STOP               = 0x0006, // The node should stop logging
    FGR_REQ_CNF_LOG_STATUS             = 0x0007, // Request the log status; the CNF message contents shall contain two uint8_t, the first a bool for log on/off, the second fgr_log_level_t
    FGR_REQ_CNF_DEBUG_LED_OFF          = 0x0008, // Switch the debug LED off, avoiding light pollution
    FGR_REQ_CNF_DEBUG_LED_ON           = 0x0009, // Switch the debug LED back on
    FGR_REQ_CNF_DEBUG_LED_BREATHE_OFF  = 0x000a, // Switch the debug LED "breathe" operation off, avoiding background light pollution
    FGR_REQ_CNF_DEBUG_LED_BREATHE_ON   = 0x000b, // Switch the debug LED "breathe" operation back on
    FGR_REQ_CNF_DEBUG_LED_STATUS       = 0x000c, // Request the debug LED status; the CNF message contents shall contain two uint8_t, the first a bool for LED on/off, the second a bool for breathe on/off
    FGR_REQ_CNF_REBOOT                 = 0x000d, // The node should reboot
    FGR_REQ_CNF_PING                   = 0x000e, // Ping the node; the CNF message contents shall contain the node's fgr_state_t, encoded as a uint8_t
    FGR_REQ_CNF_LAST                   = 0x0100
    // Request/confirmation messages beyond FGR_REQ_CNF_LAST are node specific
} fgr_req_cnf_t;

// Indication/response messages; note that the top four bits must
// be zero as they will be ORed with fgr_msg_type_t.
typedef enum {
    FGR_IND_RSP_NULL                   = 0x0000,
    FGR_IND_RSP_NEEDS_CFG              = 0x0001,  // The node has begun but has not yet been configured and so has not started
                                                  // (matches FGR_REQ_CNF_CFG)
    FGR_IND_RSP_START                  = 0x0002,  // The node has started by itself (matches FGR_REQ_CNF_START)
    FGR_IND_RSP_STOP                   = 0x0003,  // The node has stopped by itself (matches FGR_REQ_CNF_STOP)
    FGR_IND_RSP_HEARTBEAT              = 0x0004,  // Periodic heartbeat: the body shall contain a single uint8_t that is the RSSI of the WiFi link
    FGR_IND_RSP_LAST                   = 0x0100
    // Indication/response messages beyond FGR_IND_RSP_LAST are node specific
} fgr_ind_rsp_t;

// Log levels.
typedef enum {
    FGR_LOG_LEVEL_DEBUG    = 0x00,
    FGR_LOG_LEVEL_INFO     = 0x01,
    FGR_LOG_LEVEL_WARN     = 0x02,
    FGR_LOG_LEVEL_ERROR    = 0x03
} fgr_log_level_t;

// Error codes.
// IF YOU ADD AN ERROR CODE HERE don't forget to add it to the printable list in fgr_msg.c
typedef enum {
    FGR_ERROR_NONE              = 0,
    FGR_ERROR_GENERIC           = 1,
    FGR_ERROR_INVALID_REQUEST   = 2,
    FGR_ERROR_UNHANDLED_REQUEST = 3,
    FGR_ERROR_INVALID_PARAM     = 4,
    FGR_ERROR_MSG_TOO_LONG      = 5,
    FGR_ERROR_ABORTED           = 6,
    FGR_ERROR_BUSY              = 7,
    FGR_ERROR_TIMEOUT           = 8,
    FGR_ERROR_OUT_OF_RESOURCES  = 9,
    FGR_ERROR_HARDWARE          = 10
} fgr_error_t;

// States; note that this is the most generic, top-level state, indicating that a node
// has connected, been configured, has stopped for some reason or has ended up in
// a bad global state e.g. (disconnected).  Any more details states should be signalled
// in another way.
// IF YOU ADD A STATUS HERE don't forget to add it to the printable list in fgr_msg.c
// AND to the ConnectionState class in controller.py
typedef enum {
    FGR_STATE_NOT_POPULATED    = 0,
    FGR_STATE_NEEDS_CFG        = 1,   // Matches FGR_IND_RSP_NEEDS_CFG
    FGR_STATE_STARTED          = 2,   // Matches FGR_REQ_CNF_START, means configured and connected, operating normally
    FGR_STATE_STOPPED          = 3,   // Matches FGR_REQ_CNF_STOP, means configured and connected but not currently doing anything useful
    FGR_STATE_BUSY             = 4,
    FGR_STATE_GENERIC_FAILED   = 5,
    FGR_STATE_HARDWARE_FAILURE = 6,
    FGR_STATE_LAST             = 0x7f
    // States beyond FGR_STATE_LAST are node specific
} fgr_state_t;

// Request message header.
typedef struct __attribute__((packed)) {
    uint16_t type;      // fgr_req_cnf_t, top four bits ORed with FGR_MSG_TYPE_REQ
    uint8_t reference;  // Reference that may be copied into any confirmation
} fgr_msg_header_req_t;

// Confirmation message header.
typedef struct __attribute__((packed)) {
    uint16_t type;      // fgr_req_cnf_t, top four bits ORed with FGR_MSG_TYPE_CNF, big-endian
    uint8_t reference;  // Reference copied from the request being confirmed
    uint8_t error;      // fgr_error_t;
} fgr_msg_header_cnf_t;

// Indication message header.
typedef struct __attribute__((packed)) {
    uint16_t type;      // fgr_ind_rsp_t, top four bits ORed with FGR_MSG_TYPE_IND, big-endian
    uint8_t reference;  // Reference that may to copied into any response
    uint8_t state;      // fgr_state_t;
} fgr_msg_header_ind_t;

// Response message header.
typedef struct __attribute__((packed)) {
    uint16_t type;      // fgr_ind_rsp_t, top four bits ORed with FGR_MSG_TYPE_RSP, big-endian
    uint8_t reference;  // Reference copied from the indication that elicited the response
} fgr_msg_header_rsp_t;

// Log message header.
typedef struct __attribute__((packed)) {
    uint16_t type;      // zero, top four bits ORed with FGR_MSG_TYPE_LOG, big-endian
    uint8_t level;      // fgr_log_level_t
} fgr_msg_header_log_t;

// Message header.
typedef union {
  uint32_t header;
  fgr_msg_header_req_t req;
  fgr_msg_header_cnf_t cnf;
  fgr_msg_header_ind_t ind;
  fgr_msg_header_rsp_t rsp;
  fgr_msg_header_log_t log;
} fgr_msg_header_t;

// Message body.
typedef struct __attribute__((packed)) {
    uint32_t length;                            // The number of bytes to follow, big-endian
    uint8_t contents[FGR_MSG_CONTENTS_MAX_LEN]; // The message contents; when used in
                                                // fgr_log_msg_t the string shall be
                                                // null-terminated and the length
                                                // shall _not_ include the null terminator
} fgr_msg_body_t;

// Message.
typedef struct __attribute__((packed)) {
    fgr_msg_header_t header;
    fgr_msg_body_t body;
} fgr_msg_t;

#ifdef __cplusplus
}
#endif
 
/** @}*/
 
#endif // _FGR_PROTOCOL_H_
 
 // End of file
