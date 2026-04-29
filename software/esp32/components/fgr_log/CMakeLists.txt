# Log to controller
idf_component_register(SRCS "fgr_log.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_netif)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
