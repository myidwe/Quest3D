#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
bash "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
cd "$QUEST_CACHE/source/addons/nightfall-stream"
cmake --preset android -DCMAKE_BUILD_TYPE=Release -DGODOTCPP_SUFFIX=.android.template_release.arm64
cmake --build --preset android -j6
