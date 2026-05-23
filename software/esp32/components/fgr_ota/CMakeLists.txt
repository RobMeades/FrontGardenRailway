# HTTPS-based OTA server for the front garden railway

# In your application you will need to supply a ca_cert.pem (for the HTTPS server) for this component to compile,
# i.e. do something like:
#
# idf_build_get_property(project_dir PROJECT_DIR)
#
# ...and then add to your main application's idf_component_register() an extra parameter:
#
# EMBED_TXTFILES ${project_dir}/server_certs/ca_cert.pem

idf_component_register(SRCS "fgr_ota.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES esp_http_client app_update nvs_flash fgr_metrics fgr_monitor fgr_nvs)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
