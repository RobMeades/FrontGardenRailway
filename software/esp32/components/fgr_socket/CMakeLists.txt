# Networking for the front garden railway
idf_component_register(SRCS "fgr_socket.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_netif esp_timer fgr_debug fgr_util fgr_monitor fgr_task)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
