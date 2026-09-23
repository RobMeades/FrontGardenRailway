# Application to drive the big round button, a front garden railway node
idf_component_register(SRCS "big_round_button.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_event esp_driver_gpio esp_timer fgr_lib fgr_util fgr_heap fgr_monitor fgr_metrics fgr_msg fgr_ws2812 fgr_debug fgr_log fgr_ota
                       REQUIRES esp_psram
                       EMBED_TXTFILES ../../../../server_certs/ca_cert.pem)
# Force the application to compile with debug logging enabled so that we can switch it on at run-time
target_compile_definitions(${COMPONENT_LIB} PRIVATE LOG_LOCAL_LEVEL=ESP_LOG_DEBUG)
