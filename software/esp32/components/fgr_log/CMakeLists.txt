# Log to controller
idf_component_register(SRCS "fgr_log.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES nvs_flash fgr_util fgr_nvs fgr_socket fgr_msg)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
