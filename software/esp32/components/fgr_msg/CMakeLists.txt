# Messaging (with the controller) for a node in the front garden railway
idf_component_register(SRCS "fgr_msg.c"
                       INCLUDE_DIRS "api"
                       # esp_wifi is only here for esp_wifi_sta_get_rssi()
                       PRIV_REQUIRES esp_wifi fgr_util fgr_debug fgr_socket)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
