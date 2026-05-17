# Initialise and deinitialise non-volatile storage
idf_component_register(SRCS "fgr_nvs.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES nvs_flash fgr_metrics)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)