# Test application a front garden railway node
idf_component_register(SRCS "test.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_event fgr_lib fgr_util fgr_monitor fgr_metrics fgr_msg fgr_debug fgr_log
                       EMBED_TXTFILES ../../../../server_certs/ca_cert.pem)
# Force the application to compile with debug logging enabled so that we can switch it on at run-time
target_compile_definitions(${COMPONENT_LIB} PRIVATE LOG_LOCAL_LEVEL=ESP_LOG_DEBUG)
