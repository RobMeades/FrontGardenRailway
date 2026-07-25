# Utilities implementation for the front garden railway
idf_component_register(SRCS "fgr_util.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES fgr_heap)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
