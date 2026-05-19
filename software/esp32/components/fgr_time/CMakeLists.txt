# Time-related stuff for the front garden railway
idf_component_register(SRCS "fgr_time.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_timer esp_netif lwip fgr_util)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
