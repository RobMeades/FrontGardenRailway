# Monitor task implementation for the front garden railway
idf_component_register(SRCS "fgr_monitor.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_timer fgr_util fgr_task)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
