# Heap checking implementation for the front garden railway
idf_component_register(SRCS "fgr_heap.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_timer fgr_rram fgr_ota
                       REQUIRES fgr_util)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
