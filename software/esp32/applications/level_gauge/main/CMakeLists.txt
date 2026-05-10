# Water level gauge, a front garden railway node
idf_component_register(SRCS "level_gauge.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_event esp_timer esp_wifi fgr_util fgr_network fgr_msg fgr_ota fgr_socket fgr_debug fgr_log fgr_rcwl9610a
                       EMBED_TXTFILES ../../../../server_certs/ca_cert.pem)
# Force the application to compile with debug logging enabled so that we can switch it on a run-time
target_compile_definitions(${COMPONENT_LIB} PRIVATE LOG_LOCAL_LEVEL=ESP_LOG_DEBUG)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
