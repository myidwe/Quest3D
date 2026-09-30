// Actual production TLS -> bounded PCM sink -> miniaudio Null callbacks.
#include "audio/file_audio_epoch_session.h"
#include <miniaudio.h>
#include <cassert>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>
#include <cerrno>
using namespace nightfall;
using namespace nightfall::file_audio;
static pthread_t main_thread;
static std::atomic<bool> fail_main_join{false};
extern "C" int __real_pthread_join(pthread_t, void **);
extern "C" int __wrap_pthread_join(pthread_t thread, void **result) {
    if (pthread_equal(pthread_self(), main_thread) && fail_main_join.exchange(false)) return EINVAL;
    return __real_pthread_join(thread, result);
}

template<class F> void until(F predicate) {
    const auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(8);
    while (!predicate()) {
        assert(std::chrono::steady_clock::now() < limit);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}
std::string read(const std::string &path) {
    std::ifstream file(path, std::ios::binary); assert(file);
    return {std::istreambuf_iterator<char>(file), {}};
}

class ProbeSink : public FileAudioSink {
    static ProbeSink *active;
    ma_device_data_proc actual = nullptr;
    static void dispatch(ma_device *device, void *out, const void *in, ma_uint32 frames) {
        auto &self = *active;
        if (self.suppress.load()) std::memset(out, 0, size_t(frames) * 8);
        else {
            self.actual(device, out, in, frames);
            assert(self.calls < self.rows.size() && self.offset + size_t(frames) * 2 <= self.pcm.size());
            self.rows[self.calls++] = {self.offset, frames};
            std::memcpy(self.pcm.data() + self.offset, out, size_t(frames) * 8);
            self.offset += size_t(frames) * 2;
        }
        if (self.hold.exchange(false)) {
            self.held = true;
            until([&] { return self.release.load(); });
        }
    }
    int init_device(ma_device *device, const ma_device_config *config) override {
        if (fail_init) return MA_ERROR;
        const auto result = FileAudioSink::init_device(device, config);
        if (result == MA_SUCCESS) {
            assert(device->pContext->backend == ma_backend_null);
            active = this; actual = device->onData; device->onData = dispatch;
        }
        return result;
    }
public:
    std::atomic<bool> suppress{false}, hold{false}, held{false}, release{false};
    bool fail_init = false;
    struct Row { size_t offset; uint32_t frames; };
    std::vector<float> pcm = std::vector<float>(2000000);
    std::array<Row, 10000> rows{};
    size_t offset = 0, calls = 0;
    ~ProbeSink() override { release = true; close(); if (active == this) active = nullptr; }
};
ProbeSink *ProbeSink::active = nullptr;
class ProbeSession : public FileAudioEpochSession {
    std::unique_ptr<FileAudioSink> create_sink() override {
        auto sink = std::make_unique<ProbeSink>();
        sink->suppress = suppress; sink->fail_init = fail_init; current = sink.get();
        return sink;
    }
public:
    ProbeSink *current = nullptr;
    bool suppress = false, fail_init = false;
    ~ProbeSession() override { if (current) current->release = true; stop(); }
};

void exact_pcm(ProbeSink &sink, const std::vector<Consumption> &events, const std::string &pcm,
               Scope scope, AudioLifetime lifetime) {
    size_t cursor = 0, callback_at = 0; uint64_t last = 0;
    for (const auto &event : events) {
        assert(event.scope == scope && event.lifetime == lifetime);
        assert(event.callback_serial > 0 && event.callback_serial <= sink.calls);
        if (event.callback_serial != last) { assert(event.callback_serial > last); callback_at = 0; last = event.callback_serial; }
        const auto row = sink.rows[last - 1];
        assert(callback_at + event.count <= row.frames);
        const auto bytes = size_t(event.count) * 8;
        assert(cursor + bytes <= pcm.size());
        assert(std::memcmp(pcm.data() + cursor, sink.pcm.data() + row.offset + callback_at * 2, bytes) == 0);
        cursor += bytes; callback_at += event.count;
    }
    assert(cursor == pcm.size() && !events.empty() && events.back().begin.eof);
}

int main(int argc, char **argv) {
    main_thread = pthread_self();
    assert(argc == 7);
    const std::string mode = argv[5], vectors = argv[6], base = argv[1];
    TransportRequest request;
    request.url = base + "/quest3d/v1/file-audio?case=first";
    request.server_cert_pem = read(argv[2]); request.client_cert_pem = read(argv[3]); request.client_key_pem = read(argv[4]);
    request.scope.file_session.fill(0x11); request.scope.transport.fill(0x22); request.scope.channel.fill(0x33);
    request.scope.epoch = 7; request.scope.generation = 9;
    request.idle_timeout_ms = 1000; request.connect_timeout_ms = 1000;
    ProbeSession session;
    if (mode == "initfail") {
        session.fail_init = true;
        assert(!session.start(request));
        assert(session.snapshot().facts().error == EpochError::device_open);
        assert(!session.snapshot().facts().supervisor_created && !session.snapshot().facts().transport_attempted);
        assert(session.snapshot().closed());
    } else if (mode == "first-seek") {
        AudioLifetime previous;
        std::shared_ptr<const SinkWatch> previous_watch;
        for (const auto &name : {std::string("first"), std::string("seek")}) {
            if (name == "seek") {
                request.scope.epoch = 8; request.scope.generation = 10;
                request.url = base + "/quest3d/v1/file-audio?case=seek";
            }
            assert(session.start(request, 480)); // One packet capacity forces real backpressure.
            assert(!session.start(request));
            auto watch = session.watch(); const auto lifetime = watch->snapshot().facts().lifetime;
            if (previous.valid()) {
                assert(!session.stop(previous));
                assert(previous_watch->snapshot().facts().resources_released);
            }
            std::vector<Consumption> events;
            auto drain = [&] { Consumption e; while (watch->try_pop_consumption(e)) events.push_back(e); };
            until([&] { drain(); auto s = session.snapshot(); assert(s.facts().error == EpochError::none); return s.callback_eof_observed(); });
            assert(session.stop(lifetime)); drain();
            auto final = session.snapshot(); assert(final.closed() && final.callback_eof_observed());
            assert(final.facts().sink.clean_shutdown());
            assert(final.facts().sink.facts().consumed_frames == (name == "first" ? 5003U : 4043U));
            assert(final.facts().sink.facts().cancelled_frames == 0);
            exact_pcm(*session.current, events, read(vectors + "/" + name + ".pcm"), request.scope, lifetime);
            assert(!session.start(request)); // Exact scope cannot be reused.
            previous = lifetime; previous_watch = watch;
        }
    } else if (mode == "cancel" || mode == "timeout") {
        session.suppress = true;
        assert(session.start(request, 480));
        auto watch = session.watch();
        until([&] { return watch->snapshot().facts().accepted_frames == 480; });
        if (mode == "timeout") {
            until([&] { return session.snapshot().facts().supervisor_exited; });
            assert(session.snapshot().facts().error == EpochError::backpressure_timeout);
        }
        session.stop(); auto final = session.snapshot();
        assert(final.closed());
        assert(final.facts().sink.facts().accepted_frames == 480 && final.facts().sink.facts().consumed_frames == 0);
        assert(final.facts().sink.facts().cancelled_frames == 480);
        assert(!final.callback_eof_observed());
    } else if (mode == "ledger-stall") {
        request.url = base + "/quest3d/v1/file-audio?case=ledger";
        assert(session.start(request));
        until([&] { return session.snapshot().facts().supervisor_exited; });
        auto failed = session.snapshot();
        assert(failed.facts().error == EpochError::backpressure_timeout);
        assert(failed.facts().transport.state == TransportState::completed);
        assert(failed.facts().sink.facts().ledger_blocked_frames > 0);
        assert(failed.facts().sink.facts().accepted_frames == 300);
        assert(failed.facts().sink.facts().consumed_frames == 256);
        session.stop(); assert(session.snapshot().closed() && !session.snapshot().callback_eof_observed());
    } else if (mode == "join-retry") {
        assert(session.start(request));
        until([&] { return session.watch()->snapshot().facts().accepted_frames > 0; });
        const auto lifetime = session.snapshot().facts().lifetime;
        fail_main_join = true;
        assert(!session.stop(lifetime));
        assert(!session.snapshot().facts().supervisor_joined && !session.snapshot().closed());
        assert(session.stop(lifetime) && session.snapshot().closed());
    } else if (mode == "held-close") {
        assert(session.start(request));
        until([&] { return session.watch()->snapshot().facts().accepted_frames > 0; });
        session.current->hold = true;
        until([&] { return session.current->held.load(); });
        assert(session.watch()->snapshot().facts().consumed_frames > 0);
        std::atomic<bool> returned{false};
        std::thread closer([&] { session.stop(); returned = true; });
        until([&] { return !session.snapshot().facts().accepting; });
        std::this_thread::sleep_for(std::chrono::milliseconds(35));
        assert(!returned && !session.snapshot().closed());
        session.current->release = true; closer.join();
        assert(returned && session.snapshot().closed());
    } else if (mode == "bad-body" || mode == "wrong-pin") {
        if (mode == "bad-body") request.url = base + "/quest3d/v1/file-audio?case=trailing";
        assert(session.start(request));
        until([&] { return session.snapshot().facts().supervisor_exited; });
        assert(session.snapshot().facts().error == EpochError::transport_failed);
        session.stop(); assert(session.snapshot().closed() && !session.snapshot().callback_eof_observed());
        assert(!session.snapshot().facts().sink.facts().eof_accepted);
    } else assert(false);
    std::cout << "PASS " << mode << ": actual authenticated TLS and Null callback lifecycle; physical/Quest/video sync unverified\n";
}
