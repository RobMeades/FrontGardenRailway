# Test application a front garden railway node
idf_component_register(SRCS "test.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_event esp_timer esp_wifi fgr_util fgr_network fgr_ota fgr_socket fgr_msg fgr_debug fgr_log fgr_ping
                       EMBED_TXTFILES ../../../../server_certs/ca_cert.pem)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
