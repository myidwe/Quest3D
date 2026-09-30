#include "view_framing.h"
#include <cassert>
#include <limits>
#include <iostream>
int main() {
    for (int width : {1296, 1936, 2064, 2576}) {
        for (int height : {736, 1096, 1168}) {
            for (float top : {0.f, .005f, .1f, .25f}) for (float bottom : {0.f, .07f, .25f}) {
                auto l = quest3d::view_rect(0, width, height, top, bottom);
                auto r = quest3d::view_rect(1, width, height, top, bottom);
                assert(l.x == 0 && r.x == width && l.width == width && r.width == width);
                assert(l.y == r.y && l.height == r.height && l.height >= height / 2);
                assert(l.y >= 0 && l.y + l.height <= height);
                assert(l.x + l.width <= r.x && r.x + r.width <= 2 * width);
                if (top == 0 && bottom == 0) assert(l.y == 0 && l.height == height);
            }
        }
    }
    auto bad = quest3d::view_rect(0, 2064, 1168, std::numeric_limits<float>::quiet_NaN(), -1);
    assert(bad.y == 0 && bad.height == 1168);
    assert(quest3d::valid_crop(1) == .25f);
    std::cout << "view_framing PASS: paired rectangles, SBS boundaries, odd crop rows, fit, invalid inputs\n";
}
