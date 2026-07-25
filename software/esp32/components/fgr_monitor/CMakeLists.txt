# Monitor task implementation for the front garden railway
idf_component_register(SRCS "fgr_monitor.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_timer fgr_heap fgr_task fgr_msg fgr_metrics fgr_rram fgr_ota fgr_lib
                       REQUIRES fgr_util)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
