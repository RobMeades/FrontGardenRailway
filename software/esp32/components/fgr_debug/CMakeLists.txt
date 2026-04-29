# Debug utilities for the front garden railway
idf_component_register(SRCS "fgr_debug.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_driver_gpio)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
