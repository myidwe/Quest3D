cmake_minimum_required(VERSION 3.25)
project(quest3d_audio_cleanup_probe LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(GODOTCPP_BUILD_PROFILE "${CMAKE_CURRENT_SOURCE_DIR}/profile.json" CACHE FILEPATH "" FORCE)
set(GODOTCPP_TARGET template_debug CACHE STRING "" FORCE)
set(GODOTCPP_CUSTOM_API_FILE "${GODOT_CPP_SOURCE}/extension_api.json" CACHE FILEPATH "" FORCE)
add_subdirectory("${GODOT_CPP_SOURCE}" godot-cpp)
set(AUDIO_SRC "${QUEST_ROOT}/third_party/nightfall/addons/nightfall-stream")
add_library(audio_cleanup_probe SHARED probe.cpp
    "${AUDIO_SRC}/src/audio/audio_renderer.cpp"
    "${AUDIO_SRC}/src/audio/opus_decoder.cpp"
    "${AUDIO_SRC}/src/audio/miniaudio_backend.cpp")
target_compile_definitions(audio_cleanup_probe PRIVATE MA_ENABLE_ONLY_SPECIFIC_BACKENDS MA_ENABLE_NULL)
target_include_directories(audio_cleanup_probe PRIVATE "${AUDIO_SRC}/src" "${AUDIO_SRC}/include" "${OPUS_INCLUDE}" "${LIMELIGHT_INCLUDE}")
target_link_libraries(audio_cleanup_probe PRIVATE godot-cpp /lib/x86_64-linux-gnu/libopus.so.0 pthread dl m)
set_target_properties(audio_cleanup_probe PROPERTIES LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/project")
configure_file(project.godot "${CMAKE_BINARY_DIR}/project/project.godot" COPYONLY)
configure_file(audio_cleanup_probe.gdextension "${CMAKE_BINARY_DIR}/project/audio_cleanup_probe.gdextension" COPYONLY)
configure_file(test.gd "${CMAKE_BINARY_DIR}/project/test.gd" COPYONLY)
