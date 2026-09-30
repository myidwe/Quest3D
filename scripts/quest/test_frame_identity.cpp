#include "frame_identity.h"
#include <cassert>
#include <iostream>
#include <limits>
#include <mutex>
#include <thread>

using namespace quest3d;

int main() {
    const std::string epoch(32, 'a');
    assert(valid_transport_epoch(epoch));
    assert(!valid_transport_epoch(std::string(32, 'A')));
    assert(!valid_transport_epoch(std::string(31, 'a')));
    FrameIdentityMap ids;
    ids.set_transport(epoch);
    ids.begin_decoder(1);
    const auto a = ids.reserve_pts(1000000);
    const auto b = ids.reserve_pts(1000000);
    const auto c = ids.reserve_pts(999999);
    assert(a < b && b < c);
    assert(ids.submitted(a, 1000000, 0x7fffffffU, 1));
    assert(ids.submitted(b, 1000000, 0x80000000U, 1));
    assert(ids.submitted(c, 999999, 0xffffffffU, 1));
    // SurfaceTexture skipped A and B. Timestamp selection must return C,
    // never FIFO A, latest enqueue B, nearest timestamp, or signed -1.
    auto selected = ids.surface_timestamp(c * 1000);
    assert(selected && selected->frame_index == 4294967295ULL);
    assert(selected->enqueue_us == 999999 && selected->valid);
    assert(ids.surface_timestamp(b * 1000)->frame_index == 2147483648ULL);
    assert(!ids.surface_timestamp(c * 1000 + 1));
    assert(!ids.surface_timestamp((c + 1) * 1000));
    assert(!ids.surface_timestamp(0));
    assert(!ids.surface_timestamp(-1000));
    assert(!ids.submitted(b, 1000000, 17, 1));
    assert(!ids.exact_pts(b));
    assert(!ids.submitted(b, 1000000, 18, 1));
    assert(!ids.exact_pts(b));
    ids.begin_decoder(2);
    assert(!ids.surface_timestamp(c * 1000));
    auto d = ids.reserve_pts(1000000);
    assert(d > c);
    assert(!ids.submitted(d, 1000000, 4, 1)); // Callback from retired decoder.
    assert(ids.submitted(d, 1000000, 4, 2));
    assert(ids.exact_pts(d)->decoder_epoch == 2);
    ids.set_transport(std::string(32, 'b'));
    assert(!ids.exact_pts(d));
    auto e = ids.reserve_pts(1000000);
    assert(ids.submitted(e, 1000000, 0, 2));
    assert(ids.exact_pts(e)->transport_epoch == std::string(32, 'b'));
    ids.set_transport("bad epoch");
    auto f = ids.reserve_pts(1000000);
    assert(ids.submitted(f, 1000000, 0, 2));
    assert(!ids.exact_pts(f)->valid); // Video can continue, identity cannot.
    assert(ids.reserve_pts(0) == 0);
    assert(ids.reserve_pts(std::numeric_limits<int64_t>::max()) == 0);

    ids.set_transport(epoch);
    ids.begin_decoder(3);
    for (uint32_t i = 0; i < 1000; ++i) {
        auto pts = ids.reserve_pts(2000000 + i);
        assert(ids.submitted(pts, 2000000 + i, i, 3));
    }
    assert(ids.size() == FrameIdentityMap::capacity);
    assert(!ids.exact_pts(2000000));
    auto later = ids.reserve_pts(5000000);
    assert(ids.submitted(later, later, 1001, 3));
    assert(ids.size() == 1);

    FrameIdentityMap current;
    current.set_transport(std::string(32, 'a'));
    current.begin_decoder(7);
    assert(current.current(std::string(32, 'a'), 7));
    assert(!current.current(std::string(32, 'b'), 7));
    current.begin_decoder(8);
    assert(!current.current(std::string(32, 'a'), 7));
    current.set_transport("");
    assert(!current.current("", 8));

    OesLeaseState lease;
    assert(!lease.acquire());
    assert(lease.publish() == 1);
    const auto held = lease.acquire();
    assert(held == 1 && !lease.acquire() && !lease.publish());
    assert(!lease.can_update(true));
    assert(!lease.release(held + 1));
    lease.invalidate(); // Decoder restarted during a queued consumer.
    assert(!lease.can_update(true));
    assert(lease.release(held));
    assert(!lease.acquire());
    assert(!lease.can_update(false)); // CPU release is not GPU completion.
    assert(lease.can_update(true));
    assert(lease.publish() == 2);
    assert(lease.acquire() == 2);
    assert(!lease.release(held)); // Retired lease cannot release new pixels.
    assert(lease.release(2));

    // Exercise the production state helper with the same external-mutex
    // ownership used by uploader/renderer, with competing CPU consumers.
    std::mutex mutex;
    auto worker = [&] {
        for (int i = 0; i < 1000; ++i) {
            std::lock_guard<std::mutex> lock(mutex);
            assert(lease.can_update(true));
            const auto serial = lease.publish();
            assert(lease.acquire() == serial);
            assert(!lease.can_update(true));
            assert(lease.release(serial));
        }
    };
    std::thread first(worker), second(worker);
    first.join(); second.join();
    lease.fail();
    assert(!lease.can_update(true) && !lease.acquire() && !lease.publish());
    lease.invalidate();
    assert(!lease.can_update(true)); // Fatal sync failure is not a retryable timeout.
    std::cout << "PASS: production frame identity/PTS/epoch/uint32/eviction and OES lease state\n";
}
