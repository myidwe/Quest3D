#include "network/file_audio_transport.h"
#include <cassert>
#include <condition_variable>
#include <fstream>
#include <iostream>
#include <iterator>
#include <chrono>
#include <stdexcept>
#include <deque>
#include <thread>
#include <cerrno>

static std::atomic_bool fail_next_worker{false};
static std::atomic_bool fail_next_join{false};
extern "C" int __real_pthread_create(pthread_t*, const pthread_attr_t*, void* (*)(void*), void*);
extern "C" int __real_pthread_join(pthread_t, void**);
extern "C" int __wrap_pthread_create(pthread_t* thread, const pthread_attr_t* attr, void* (*entry)(void*), void* arg) {
    if (fail_next_worker.exchange(false)) return EAGAIN;
    return __real_pthread_create(thread, attr, entry, arg);
}
extern "C" int __wrap_pthread_join(pthread_t thread, void** result) {
    if (fail_next_join.exchange(false)) return EINVAL;
    return __real_pthread_join(thread, result);
}

using namespace nightfall::file_audio;
static Scope scope() {
    Scope result;
    result.file_session.fill(1); result.transport.fill(2); result.channel.fill(3);
    result.epoch = UINT64_MAX; result.generation = 17;
    return result;
}
static std::string read(const char* path) {
    std::ifstream stream(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
}
static void generate(const char* path) {
    std::ofstream file(path, std::ios::binary);
    for (int i = 0; i < 3; ++i) {
        Block block; block.scope = scope(); block.sequence = i; block.block_id = 100 + i;
        block.pts_ns = i == 0 ? 0 : i == 1 ? 10000000 : 10354167;
        if (i == 2) block.flags = FLAG_EOF;
        else {
            block.count = block.total_samples = i == 0 ? 480 : 17;
            for (size_t n = 0; n < size_t(block.count) * 2; ++n) block.samples[n] = (int(n % 31) - 15) / 32.0f;
        }
        auto bytes = encode(block); file.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
    }
}

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "generate") { generate(argv[2]); return 0; }
    assert(argc == 7 || argc == 8);
    const std::string mode = argv[6];
    TransportRequest request;
    request.url = argv[1]; request.server_cert_pem = read(argv[2]);
    request.client_cert_pem = read(argv[3]); request.client_key_pem = read(argv[4]); request.scope = scope();
    request.connect_timeout_ms = 1000; request.idle_timeout_ms = 500;
    const bool actual = mode == "actual-first" || mode == "actual-seek";
    std::string expected_pcm;
    if (actual) {
        assert(argc == 8); expected_pcm = read(argv[7]);
        request.scope.file_session.fill(0x11); request.scope.transport.fill(0x22); request.scope.channel.fill(0x33);
        request.scope.epoch = mode == "actual-first" ? 7 : 8;
        request.scope.generation = mode == "actual-first" ? 9 : 10;
    }
    const bool invalid = mode == "invalid" || mode == "invalid-scope" || mode == "invalid-cert";
    if (mode == "invalid") request.url = "http://127.0.0.1/forbidden";
    if (mode == "invalid-scope") request.scope.file_session.fill(0);
    if (mode == "invalid-cert") request.client_cert_pem.clear();
    FileAudioTransport transport;
    std::mutex mutex;
    std::condition_variable condition;
    size_t delivered = 0, pcm_frames = 0;
    bool entered = false, released = false, exited = false, eof = false;
    std::deque<Block> queue; // Fixture's bounded one-block consumer queue.
    bool queue_waiting = false;
    std::atomic_bool stop_returned{false};
    const FileAudioTransport::OnBlock consume = [&](const Block& block, const std::atomic_bool& cancel) {
        assert(block.scope == request.scope);
        assert(block.sequence == delivered);
#if defined(__cpp_exceptions) || defined(_CPPUNWIND)
        if (mode == "throw") throw std::runtime_error("fixture consumer failure");
#endif
        if (mode == "reject") return false;
        if (mode == "selfstop") { assert(!transport.stop()); return false; }
        if (mode == "backpressure" || mode == "backpressurecancel") {
            std::unique_lock<std::mutex> lock(mutex);
            const auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            while (!queue.empty()) {
                queue_waiting = true; condition.notify_all();
                if (cancel.load()) return false;
                assert(std::chrono::steady_clock::now() < until);
                condition.wait_for(lock, std::chrono::milliseconds(10));
            }
            if (cancel.load()) return false;
            queue.push_back(block); assert(queue.size() == 1); condition.notify_all();
        }
        {
            std::unique_lock<std::mutex> lock(mutex);
            entered = true; condition.notify_all();
            if (mode == "hold" && delivered == 0) {
                // Deliberately hold the actual callback beyond cancellation to
                // verify no joined/cleanup receipt appears before real return.
                condition.wait(lock, [&] { return released; });
                assert(cancel.load());
                exited = true;
                return false;
            }
            if (mode == "cancel" && delivered == 0) {
                // Real cancellable backpressure; deadline is a test failure,
                // never an affirmative receive/cleanup acknowledgement.
                auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
                while (!cancel.load()) {
                    lock.unlock(); std::this_thread::yield(); lock.lock();
                    assert(std::chrono::steady_clock::now() < until);
                }
                exited = true;
                return false;
            }
        }
        if (block.eof()) {
            eof = true;
            if (actual) assert(block.pts_ns == 104229167);
        }
        else {
            if (actual) {
                const auto bytes = size_t(block.count) * 8;
                assert(pcm_frames * 8 + bytes <= expected_pcm.size());
                assert(std::memcmp(block.samples.data(), expected_pcm.data() + pcm_frames * 8, bytes) == 0);
                assert(block.discontinuity == (delivered == 0 ? (mode == "actual-first" ? 1 : 2) : 0));
                assert(block.pts_ns == int64_t(delivered * 10000000 + (mode == "actual-first" ? 0 : 20000000)));
            } else {
                assert(block.count == (delivered == 0 ? 480 : 17));
                for (size_t n = 0; n < size_t(block.count) * 2; ++n)
                    assert(block.samples[n] == (int(n % 31) - 15) / 32.0f);
            }
            pcm_frames += block.count;
        }
        ++delivered;
        if (mode == "incremental" && delivered == 1) std::cout << "FIRST_BLOCK" << std::endl;
        return true;
    };
    if (mode == "workerfail") fail_next_worker.store(true);
    const bool started = transport.start(request, consume);
    if (invalid) {
        assert(!started && transport.snapshot().error == TransportError::invalid_request);
    } else if (mode == "workerfail") {
        assert(!started && transport.snapshot().error == TransportError::worker_create);
        assert(transport.snapshot().worker_error == EAGAIN && !transport.snapshot().worker_created);
    } else {
        assert(started);
        assert(!transport.start(request, [](const Block&, const std::atomic_bool&) { return true; }));
    }
    if (mode == "hold" || mode == "cancel") {
        std::unique_lock<std::mutex> lock(mutex);
        assert(condition.wait_for(lock, std::chrono::seconds(5), [&] { return entered; }));
        std::thread stopper([&] { assert(transport.stop()); stop_returned.store(true); });
        if (mode == "hold") {
            // Wait for the stop request to reach the actual run without timers.
            // Stop cannot join while this held callback still owns the block.
            lock.unlock();
            auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            while (!transport.snapshot().cancel_requested) {
                assert(std::chrono::steady_clock::now() < until); std::this_thread::yield();
            }
            auto held = transport.snapshot();
            assert(held.callback_active && !held.worker_exited && !held.joined && !stop_returned.load());
            lock.lock(); released = true; condition.notify_all();
        }
        lock.unlock(); stopper.join(); assert(exited);
    } else if (mode == "backpressure" || mode == "backpressurecancel") {
        std::unique_lock<std::mutex> lock(mutex);
        assert(condition.wait_for(lock, std::chrono::seconds(5), [&] { return queue_waiting; }));
        assert(transport.snapshot().callback_active && queue.size() == 1);
        if (mode == "backpressurecancel") {
            lock.unlock(); assert(transport.stop());
        } else {
            size_t drained = 0;
            while (true) {
                assert(condition.wait_for(lock, std::chrono::seconds(5), [&] { return !queue.empty(); }));
                const auto block = queue.front(); queue.pop_front(); condition.notify_all();
                assert(block.sequence == drained++);
                if (block.eof()) break;
            }
            assert(drained == 3); lock.unlock();
            const auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
            while (!transport.snapshot().worker_exited) {
                assert(std::chrono::steady_clock::now() < until); std::this_thread::yield();
            }
            assert(transport.stop());
        }
    } else if (mode == "idlecancel") {
        auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (transport.snapshot().state == TransportState::starting) {
            assert(std::chrono::steady_clock::now() < until); std::this_thread::yield();
        }
        assert(transport.stop());
    } else {
        auto until = std::chrono::steady_clock::now() + std::chrono::seconds(8);
        while (!transport.snapshot().worker_exited) {
            assert(std::chrono::steady_clock::now() < until); std::this_thread::yield();
        }
        if (mode == "joinfail") {
            fail_next_join.store(true);
            assert(!transport.stop());
            assert(!transport.snapshot().joined && transport.snapshot().worker_error == EINVAL);
        }
        assert(transport.stop());
    }
    const auto result = transport.snapshot();
    std::cerr << "transport state=" << int(result.state) << " error=" << int(result.error)
              << " http=" << result.http_status << " curl=" << result.curl_code
              << " bytes=" << result.response_bytes << " records=" << delivered << std::endl;
    assert(result.joined && result.worker_exited && !result.callback_active);
    const auto expected = std::string(argv[5]);
    if (expected == "complete") {
        assert(result.state == TransportState::completed && eof);
        if (actual) assert(pcm_frames * 8 == expected_pcm.size() && delivered == (mode == "actual-first" ? 12 : 10));
        else assert(delivered == 3 && pcm_frames == 497);
    }
    else if (expected == "cancel") assert(result.state == TransportState::cancelled && !eof);
    else assert(result.state == TransportState::failed && !eof);
    if (mode == "fragment") assert(result.body_callbacks > 1);
    if (mode == "reject") assert(result.error == TransportError::consumer_rejected);
    if (mode == "throw") assert(result.error == TransportError::callback_exception);
    if (mode == "hold" || mode == "cancel") assert(delivered == 0);
    assert(transport.stop()); // Idempotent real join result.
    if (mode == "restart") {
        // Explicit new epoch after real stop/join, never an implicit retry of
        // the prior response or reuse of its easy handle/TLS connection.
        const auto old = result;
        request.scope.epoch = 42;
        delivered = pcm_frames = 0; eof = false;
        assert(transport.start(request, consume));
        auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (!transport.snapshot().worker_exited) {
            assert(std::chrono::steady_clock::now() < until); std::this_thread::yield();
        }
        assert(transport.stop());
        const auto next = transport.snapshot();
        assert(next.state == TransportState::completed && next.attempt == old.attempt + 1);
        assert(next.scope.epoch == 42 && old.scope.epoch == UINT64_MAX && old.joined);
        assert(next.joined && next.worker_exited && delivered == 3 && pcm_frames == 497 && eof);
    }
    std::cout << "{\"state\":" << int(result.state) << ",\"error\":" << int(result.error)
              << ",\"http\":" << result.http_status << ",\"curl\":" << result.curl_code
              << ",\"worker_error\":" << result.worker_error
              << ",\"bytes\":" << result.response_bytes << ",\"callbacks\":" << result.body_callbacks
              << ",\"records\":" << delivered << ",\"pcm_frames\":" << pcm_frames << ",\"joined\":true}" << std::endl;
}
