# Inside version_injection.cmake

if(NOT DEFINED PROJECT_VER)
    # Read the application version
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/version.txt")
        file(READ "${CMAKE_CURRENT_SOURCE_DIR}/version.txt" APP_VER)
        string(STRIP "${APP_VER}" APP_VER)
    else()
        set(APP_VER "1.0")
    endif()

    # Read the system version
    set(SYS_VER_PATH "${CMAKE_CURRENT_LIST_DIR}/system_version.txt")
    if(EXISTS "${SYS_VER_PATH}")
        file(READ "${SYS_VER_PATH}" SYS_VER)
        string(STRIP "${SYS_VER}" SYS_VER)
    else()
        set(SYS_VER "1.0")
    endif()

    # Extract an ultra-short 5-character Git Hash
    execute_process(
        COMMAND git rev-parse --short=5 HEAD
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        OUTPUT_VARIABLE GIT_HASH
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(NOT GIT_HASH)
        set(GIT_HASH "unkwn")
    endif()

    # Check for uncommitted changes ('x' marker indicates dirty)
    execute_process(
        COMMAND git status --porcelain
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        OUTPUT_VARIABLE GIT_STATUS_OUTPUT
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(NOT "${GIT_STATUS_OUTPUT}" STREQUAL "")
        set(GIT_HASH "${GIT_HASH}x")
    endif()

    # =========================================================================
    # HIGH-SPEED COMPACT TIMING NONCE
    # =========================================================================
    # %M%S grabs the current Minute and Second (e.g., 45 mins, 22 secs -> 4522)
    # It changes every single second, but only consumes 4 characters total
    string(TIMESTAMP DEV_NONCE "%M%S")

    # Construct the highly optimized development version string
    # Length Example: v1.0.1.0.d4522_88c38x (22 characters)
    set(PROJECT_VER "v${APP_VER}.${SYS_VER}.d${DEV_NONCE}_${GIT_HASH}")
endif()

message(STATUS "### version applied: ${PROJECT_VER} ###")
add_compile_definitions(CONFIG_APP_PROJECT_VERSION="${PROJECT_VER}")