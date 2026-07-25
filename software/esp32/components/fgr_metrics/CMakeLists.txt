# Metrics implementation for the front garden railway
idf_component_register(SRCS "fgr_metrics.c"
                       INCLUDE_DIRS "api"
                       # esp_wifi is only here for esp_wifi_sta_get_rssi()
                       PRIV_REQUIRES esp_timer esp_wifi fgr_util fgr_heap fgr_monitor fgr_task fgr_rram fgr_time fgr_network)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)

# Add the cJSON source file directly
target_sources(${COMPONENT_LIB} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../../third_party/cjson/cJSON.c")

# Add the CJSON include directory directly
target_include_directories(${COMPONENT_LIB} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../../third_party/cjson")