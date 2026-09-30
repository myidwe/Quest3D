#include "submission_identity.h"
#include <cassert>
#include <iostream>
#include <mutex>
#include <thread>
using namespace quest3d;

int main() {
    SwapchainImageState image;
    assert(!image.needs_render(false));
    assert(image.needs_render(true));
    image.acquired(4);
    // xrWaitSwapchainImage timeout retains acquisition. Any number of static
    // submit_frame(false) calls still re-enter the actual renderer's wait path.
    for (int frame = 0; frame < 72; ++frame) {
        assert(image.needs_render(false));
        assert(image.acquired() && image.index() == 4); // no second acquire
    }
    image.released(); // only after wait success + draw + release (or teardown)
    assert(!image.needs_render(false));

    const SubmissionToken token{std::string(32, 'a'), 7, 0xffffffffU, 23};
    const uint64_t focused = 16 + 5;
    SubmissionIdentity proof;
    auto ready = [&](uint64_t cycle = 1, int64_t pixel = 1'000'000) {
        proof.begin(cycle, 99, focused, 3, true);
        assert(proof.observe_eye(0, 101, token, pixel));
        assert(proof.observe_eye(1, 102, token, pixel));
    };
    auto finish = [&](int64_t result = 0, std::vector<uintptr_t> layers = {101, 102}) {
        return proof.finish(1, 99, focused, 3, result, layers, 1'000'100, true);
    };
    ready(); assert(finish());
    assert(proof.current(1'000'101, 3, true));
    assert(proof.token() == token && proof.token().frame == 0xffffffffU);
    assert(!proof.current(1'000'101, 4, true));
    assert(!proof.current(1'000'101, 3, false)); // decoder/nonce/source replaced
    assert(!proof.current(1'000'099, 3, true)); // monotonic clock cannot move backwards
    assert(!proof.current(1'250'101, 3, true)); // last success is not indefinitely usable
    // A new cycle without an xrEndFrame invalidates old success immediately.
    proof.begin(2, 99, focused, 3, true);
    assert(!proof.current(1'000'102, 3, true));
    for (int result : {-1, 1, 3, 9}) { // errors, timeout, loss-pending, discarded
        ready(); assert(!finish(result)); assert(!proof.current(1'000'101, 3, true));
    }
    for (auto layers : {std::vector<uintptr_t>{}, {101}, {102}, {101, 102, 101}, {101, 203}}) {
        ready(); assert(!finish(0, layers));
    }
    ready(); assert(finish(0, {777, 102, 101})); // sorting + unrelated overlay are harmless
    for (uint64_t state : {uint64_t(0), uint64_t(16 + 4), uint64_t(16 + 3), uint64_t(16 + 6)}) {
        proof.begin(1, 99, state, 3, true); // visible but unfocused, hidden, stopping
        assert(!proof.observe_eye(0, 101, token, 1'000'000));
        assert(!finish());
    }
    proof.begin(1, 99, focused, 3, false); // shouldRender=false
    assert(!finish());
    ready(); assert(!proof.finish(2, 99, focused, 3, 0, {101, 102}, 1'000'100, true));
    ready(); assert(!proof.finish(1, 100, focused, 3, 0, {101, 102}, 1'000'100, true));
    ready(); assert(!proof.finish(1, 99, focused + 16, 3, 0, {101, 102}, 1'000'100, true));
    ready(); assert(!proof.finish(1, 99, focused, 4, 0, {101, 102}, 1'000'100, true));
    ready(); assert(!proof.finish(1, 99, focused, 3, 0, {101, 102}, 1'000'100, false));
    for (int which = 0; which < 4; ++which) {
        auto other = token;
        if (which == 0) other.lease++;
        if (which == 1) other.decoder++;
        if (which == 2) other.frame = 0x80000000U;
        if (which == 3) other.transport[0] = 'b';
        proof.begin(1, 99, focused, 3, true);
        assert(proof.observe_eye(0, 101, token, 1'000'000));
        assert(proof.observe_eye(1, 102, other, 1'000'000));
        assert(!finish());
    }
    ready(); assert(!proof.observe_eye(0, 103, token, 1'000'000)); assert(!finish());
    ready(); proof.invalidate(); assert(!finish()); // explicit stop/state event
    ready(); assert(finish());
    // Reusing an unchanged image in later successful submissions does not renew pixel age.
    ready(2);
    assert(proof.finish(2, 99, focused, 3, 0, {101, 102}, 1'400'000, true));
    assert(!proof.current(1'500'001, 3, true));
    ready(3);
    assert(!proof.finish(3, 99, focused, 3, 0, {101, 102}, 1'500'001, true));
    auto invalid = token; invalid.transport[0] = 'A';
    proof.begin(1, 99, focused, 3, true);
    assert(!proof.observe_eye(0, 101, invalid, 1'000'000));
    std::mutex lock;
    auto worker = [&] {
        for (int i = 0; i < 2000; ++i) {
            std::lock_guard<std::mutex> held(lock);
            ready(); assert(finish()); proof.invalidate();
        }
    };
    std::thread first(worker), second(worker); first.join(); second.join();
    std::cout << "PASS: production xrEndFrame cycle/actual layers/epochs/strict success/TTL/pixel age/invalidation\n";
}
