#pragma once
#include <algorithm>
#include <cmath>

namespace quest3d {
struct ViewRect { int x, y, width, height; };
inline float valid_crop(float value) {
    return std::isfinite(value) ? std::clamp(value, 0.0f, 0.25f) : 0.0f;
}
// One already-separated eye; never crop the packed SBS canvas as a whole.
inline ViewRect view_rect(int eye, int width, int height, float top, float bottom) {
    const int first = int(std::lround(height * valid_crop(top)));
    const int last = int(std::lround(height * valid_crop(bottom)));
    return {eye * width, first, width, std::max(1, height - first - last)};
}
}
