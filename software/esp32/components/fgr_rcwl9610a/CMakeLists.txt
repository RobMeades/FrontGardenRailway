# Get distance measurements from an RCWL-9610A chip over UART
idf_component_register(SRCS "fgr_rcwl9610a.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_driver_uart fgr_heap)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
