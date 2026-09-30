#include "video/stream_start_gate.h"
#include <cassert>
#include <chrono>
#include <future>
#include <iostream>
#include <thread>

using godot::StreamStartGate;
using namespace std::chrono_literals;

int main() {
    StreamStartGate gate;
    assert(!gate.wait_for_start(10s)); // Never-started objects are already stopped.
    assert(!gate.mark_started());
    for (int i = 0; i < 100; ++i) {
        gate.reset();
        std::promise<void> entering;
        auto entered = entering.get_future();
        auto decoder = std::async(std::launch::async, [&] {
            entering.set_value();
            return gate.wait_for_start(10s);
        });
        entered.wait();
        gate.stop();
        // Bound the regression test itself: cancellation must wake the actual
        // production wait, not merely shorten its unchanged 10-second timeout.
        assert(decoder.wait_for(1s) == std::future_status::ready);
        assert(!decoder.get());
        assert(!gate.mark_started()); // A late start callback cannot revive it.
        assert(!gate.wait_for_start(10s));

        gate.reset();
        auto connected = std::async(std::launch::async, [&] { return gate.wait_for_start(10s); });
        assert(gate.mark_started());
        assert(connected.wait_for(1s) == std::future_status::ready && connected.get());
        gate.stop();
        assert(!gate.wait_for_start(10s));
    }
    gate.reset();
    assert(!gate.wait_for_start(0ms)); // Timeout alone is not a successful start.
    gate.stop();
    std::cout << "PASS production StreamStartGate: 100 cancel/restart cycles; no network or Quest tested\n";
}
