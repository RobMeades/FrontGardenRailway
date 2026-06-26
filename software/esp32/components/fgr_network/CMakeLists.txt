# Networking for the front garden railway
idf_component_register(SRCS "fgr_network.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_wifi esp_phy fgr_heap fgr_metrics fgr_monitor fgr_util)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
