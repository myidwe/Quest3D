cmake_minimum_required(VERSION 3.25)
project(quest_stream_retirement_fixture LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(GODOTCPP_BUILD_PROFILE "${CMAKE_CURRENT_SOURCE_DIR}/profile.json" CACHE FILEPATH "" FORCE)
set(GODOTCPP_TARGET template_debug CACHE STRING "" FORCE)
set(GODOTCPP_CUSTOM_API_FILE "${GODOT_CPP_SOURCE}/extension_api.json" CACHE FILEPATH "" FORCE)
add_subdirectory("${GODOT_CPP_SOURCE}" godot-cpp)
set(STREAM "${QUEST_ROOT}/third_party/nightfall/addons/nightfall-stream")
add_library(stream_retirement_probe SHARED probe.cpp moonlight.cpp companions.cpp
  "${STREAM}/src/nightfall_stream.cpp"
  "${STREAM}/src/video/stream_connection.cpp"
  "${STREAM}/src/audio/audio_renderer.cpp"
  "${STREAM}/src/audio/opus_decoder.cpp"
  "${STREAM}/src/audio/miniaudio_backend.cpp")
target_compile_definitions(stream_retirement_probe PRIVATE MA_ENABLE_ONLY_SPECIFIC_BACKENDS MA_ENABLE_NULL LI_HAS_CONNECTION_CALLBACK_DRAIN=1)
target_compile_options(stream_retirement_probe PRIVATE -Wall -Wextra -Wno-unused-parameter -include "${CMAKE_CURRENT_SOURCE_DIR}/moonlight_api.h")
option(STREAM_RETIREMENT_SANITIZE "Instrument production stream/audio fixture sources" OFF)
if(STREAM_RETIREMENT_SANITIZE)
  target_compile_definitions(stream_retirement_probe PRIVATE ASAN_ENABLED UBSAN_ENABLED)
  target_compile_options(stream_retirement_probe PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(stream_retirement_probe PRIVATE -fsanitize=address,undefined)
endif()
target_include_directories(stream_retirement_probe PRIVATE "${STREAM}/src" "${STREAM}/include" "${OPUS_INCLUDE}" "${LIMELIGHT_INCLUDE}" "${FFMPEG_INCLUDE}")
target_link_libraries(stream_retirement_probe PRIVATE godot-cpp /lib/x86_64-linux-gnu/libopus.so.0 /lib/x86_64-linux-gnu/libavcodec.so.58 /lib/x86_64-linux-gnu/libavutil.so.56 pthread dl m)
set_target_properties(stream_retirement_probe PROPERTIES LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/project")
foreach(file project.godot stream_retirement_probe.gdextension test.gd test_start_refusal.gd)
  configure_file("${file}" "${CMAKE_BINARY_DIR}/project/${file}" COPYONLY)
endforeach()
