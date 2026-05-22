# General library functions for the front garden railway
idf_component_register(SRCS "fgr_lib.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_wifi fgr_util fgr_task fgr_monitor fgr_time fgr_metrics fgr_network fgr_nvs fgr_ota fgr_socket fgr_msg fgr_debug fgr_log)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
