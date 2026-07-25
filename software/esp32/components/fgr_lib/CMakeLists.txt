# General library functions for the front garden railway
idf_component_register(SRCS "fgr_lib.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_wifi fgr_task fgr_monitor fgr_time fgr_metrics fgr_network fgr_nvs fgr_socket fgr_debug fgr_log
                       REQUIRES fgr_heap fgr_util fgr_msg fgr_ota)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
