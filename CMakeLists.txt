cmake_minimum_required(VERSION 3.20)

set(COREGUARD_VERSION "" CACHE STRING
    "Owner-approved numeric release version; empty keeps the local development artifact")

if(COREGUARD_VERSION STREQUAL "")
    project(coreguard C)
    set(COREGUARD_VERSIONED_BUILD FALSE)
else()
    if(NOT COREGUARD_VERSION MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
        message(FATAL_ERROR
            "COREGUARD_VERSION must be a numeric major.minor.patch version")
    endif()
    project(coreguard VERSION ${COREGUARD_VERSION} LANGUAGES C)
    set(COREGUARD_VERSIONED_BUILD TRUE)
endif()

include(CheckCCompilerFlag)
include(CMakePackageConfigHelpers)
include(GNUInstallDirs)

if(NOT WIN32)
    message(FATAL_ERROR "coreguard Wave 1 targets Windows 11")
endif()

add_library(coreguard STATIC
    src/process.c
    src/platform/windows_process.c
)

add_executable(coreguard_cli src/main.c)
set_target_properties(coreguard_cli PROPERTIES OUTPUT_NAME coreguard)

target_include_directories(coreguard
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_include_directories(coreguard_cli PRIVATE include src)
target_compile_features(coreguard PUBLIC c_std_11)
target_compile_features(coreguard_cli PRIVATE c_std_11)
target_compile_definitions(coreguard PRIVATE _CRT_SECURE_NO_WARNINGS UNICODE _UNICODE)
target_compile_definitions(coreguard_cli PRIVATE _CRT_SECURE_NO_WARNINGS UNICODE _UNICODE)
target_link_libraries(coreguard_cli PRIVATE coreguard)

foreach(target IN ITEMS coreguard coreguard_cli)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /WX)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Werror)
        foreach(flag IN ITEMS -Wconversion -Wsign-conversion -Wshadow)
            string(REPLACE "-" "_" flag_id "${flag}")
            check_c_compiler_flag("${flag}" "COREGUARD_HAS${flag_id}")
            if(COREGUARD_HAS${flag_id})
                target_compile_options(${target} PRIVATE "${flag}")
            endif()
        endforeach()
    endif()
endforeach()

if(MSVC)
    check_c_compiler_flag("/analyze" COREGUARD_MSVC_HAS_ANALYZE)
    if(COREGUARD_MSVC_HAS_ANALYZE)
        # The current Windows SDK emits C28301 for unannotated SDK declarations.
        # Suppress only that external-header diagnostic; keep /WX for coreguard.
        target_compile_options(coreguard PRIVATE /analyze /wd28301)
        target_compile_options(coreguard_cli PRIVATE /analyze /wd28301)
    endif()
endif()

install(
    TARGETS coreguard
    EXPORT coreguardTargets
    ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
)

install(
    TARGETS coreguard_cli
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)

install(
    FILES ${CMAKE_CURRENT_SOURCE_DIR}/include/coreguard.h
    DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}
)

set(COREGUARD_CMAKE_PACKAGE_DIR "${CMAKE_INSTALL_LIBDIR}/cmake/coreguard")

configure_package_config_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/cmake/coreguardConfig.cmake.in
    ${CMAKE_CURRENT_BINARY_DIR}/coreguardConfig.cmake
    INSTALL_DESTINATION ${COREGUARD_CMAKE_PACKAGE_DIR}
)

install(
    EXPORT coreguardTargets
    FILE coreguardTargets.cmake
    NAMESPACE coreguard::
    DESTINATION ${COREGUARD_CMAKE_PACKAGE_DIR}
)

install(
    FILES ${CMAKE_CURRENT_BINARY_DIR}/coreguardConfig.cmake
    DESTINATION ${COREGUARD_CMAKE_PACKAGE_DIR}
)

foreach(COREGUARD_GOVERNANCE_FILE IN ITEMS LICENSE NOTICE)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${COREGUARD_GOVERNANCE_FILE}")
        install(
            FILES ${CMAKE_CURRENT_SOURCE_DIR}/${COREGUARD_GOVERNANCE_FILE}
            DESTINATION .
        )
    endif()
endforeach()

if(COREGUARD_VERSIONED_BUILD)
    write_basic_package_version_file(
        ${CMAKE_CURRENT_BINARY_DIR}/coreguardConfigVersion.cmake
        VERSION ${PROJECT_VERSION}
        COMPATIBILITY ExactVersion
    )
    install(
        FILES ${CMAKE_CURRENT_BINARY_DIR}/coreguardConfigVersion.cmake
        DESTINATION ${COREGUARD_CMAKE_PACKAGE_DIR}
    )
endif()

set(CPACK_GENERATOR "ZIP")
set(CPACK_PACKAGE_NAME "coreguard")
set(CPACK_PACKAGE_VENDOR "Coreguard")
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY
    "Coreguard Windows x64 MSVC static library and CLI")
if(COREGUARD_VERSIONED_BUILD)
    set(CPACK_PACKAGE_VERSION "${COREGUARD_VERSION}")
    set(CPACK_PACKAGE_FILE_NAME
        "coreguard-${COREGUARD_VERSION}-windows-x64-msvc")
else()
    # No authoritative project or Git release version exists yet. The local
    # proof archive is explicitly a development artifact.
    set(CPACK_PACKAGE_VERSION "dev")
    set(CPACK_PACKAGE_FILE_NAME "coreguard-dev-windows-x64-msvc")
endif()
set(CPACK_INCLUDE_TOPLEVEL_DIRECTORY FALSE)
include(CPack)
