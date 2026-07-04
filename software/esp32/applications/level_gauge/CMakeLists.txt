# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# DESTROY THE CMake CACHE - it lies, it cheats, it steals hours of your life
# This forces a clean configuration EVERY time you run CMake
if(EXISTS "${CMAKE_BINARY_DIR}/CMakeCache.txt")
    file(REMOVE "${CMAKE_BINARY_DIR}/CMakeCache.txt")
    file(REMOVE_RECURSE "${CMAKE_BINARY_DIR}/CMakeFiles")
endif()

# Set default target ONLY if not overridden by environment or command line
if(NOT DEFINED ENV{IDF_TARGET} AND NOT DEFINED IDF_TARGET)
    set(IDF_TARGET "esp32s3" CACHE STRING "Target chip" FORCE)
endif()

# If the automation script has not passed in overrides, provide defaults
if(NOT DEFINED SDKCONFIG_DEFAULTS)
    # Force regenerate sdkconfig if it has wrong chip target
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig")
        file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig" CURRENT_TARGET REGEX "^CONFIG_IDF_TARGET=")
        if(CURRENT_TARGET AND NOT CURRENT_TARGET MATCHES "esp32s3")
            message(STATUS "⚠️  sdkconfig has wrong target (${CURRENT_TARGET}), removing to force regeneration")
            file(REMOVE "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig")
            file(REMOVE "${CMAKE_CURRENT_SOURCE_DIR}/sdkconfig.old")
        endif()
    endif()
    set(SDKCONFIG_DEFAULTS
        "${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/defaults"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/chip_esp32s3"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/flash_4mbyte"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/led_multicolour"
    )

    # The location of the partitions file for 4 Mbyte flash:
    # this mess is necessary because kconfig removes relative
    # paths, they have to come in through CMake via a back door
    set(GENERATED_OVERRIDES "${CMAKE_BINARY_DIR}/sdkconfig.auto_paths.defaults")
    file(WRITE "${GENERATED_OVERRIDES}"
        "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME=\"${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/flash_4mbyte_partitions.csv\"\n"
        "CONFIG_PARTITION_TABLE_FILENAME=\"${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/flash_4mbyte_partitions.csv\"\n"
    )
    list(APPEND SDKCONFIG_DEFAULTS "${GENERATED_OVERRIDES}")
endif()

# Add compilation options globally for all targets
add_compile_options(-Wall -Wextra -Werror)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
set(EXTRA_COMPONENT_DIRS "../../components")
# Tell the linker to wrap the panic handler
idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=esp_panic_handler" APPEND)
# "Trim" the build. Include the minimal set of components, main, and anything it depends on.
idf_build_set_property(MINIMAL_BUILD ON)
# Include the version generation script; HAS to be here or the version gets over-written by ESP-IDF
include("${CMAKE_CURRENT_SOURCE_DIR}/../../sdkconfig_fragments/version_injection.cmake")
project(level_gauge)
