# Messaging (with the controller) for a node in the front garden railway
idf_component_register(SRCS "fgr_msg.c"
                       INCLUDE_DIRS "api"
                       PRIV_REQUIRES fgr_util fgr_task fgr_debug fgr_metrics fgr_socket)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
