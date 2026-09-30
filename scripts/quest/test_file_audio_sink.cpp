// Real production FileAudioSink, pinned miniaudio Null, no physical audio device.
#include "audio/file_audio_sink.h"
#include "audio/miniaudio_backend.h"
#include <miniaudio.h>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <thread>
#include <type_traits>
#include <vector>

using namespace nightfall;
using namespace nightfall::file_audio;
static_assert(!std::is_constructible<SinkSnapshot, SinkFacts>::value);
static_assert(!std::is_constructible<SinkWatch, AudioLifetime, Scope, uint32_t>::value);

template<class Predicate> void until(Predicate done) {
    auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!done()) { assert(std::chrono::steady_clock::now() < limit); std::this_thread::yield(); }
}

Scope scope(uint8_t n = 1) {
    Scope value; value.file_session[0] = n; value.transport[0] = n + 1; value.channel[0] = n + 2;
    value.epoch = 17; value.generation = n;
    return value;
}
int64_t ns(uint64_t frames) { return int64_t(frames * 1000000000ULL / 48000); }
Block block(Scope identity, uint64_t seq, uint64_t id, int64_t pts, uint16_t total,
            uint16_t offset, uint16_t count, uint8_t discontinuity = 0) {
    Block value; value.scope = identity; value.sequence = seq; value.block_id = id;
    value.pts_ns = pts; value.total_samples = total; value.offset_samples = offset;
    value.count = count; value.discontinuity = discontinuity;
    for (size_t i = 0; i < size_t(count) * CHANNELS; ++i)
        value.samples[i] = float(0.1 + (id % 1000) * 0.0001 + (offset * 2 + i) * 0.000001);
    return value;
}
Block eof(Scope identity, uint64_t seq, uint64_t id, int64_t pts) {
    Block value; value.scope = identity; value.sequence = seq; value.block_id = id;
    value.pts_ns = pts; value.flags = FLAG_EOF; return value;
}

class ProbeSink : public FileAudioSink {
    static ProbeSink *active;
    ma_device_data_proc actual_data = nullptr;
    static void dispatch(ma_device *device, void *output, const void *input, ma_uint32 frames) {
        auto *self = active;
        assert(self && self->device == device);
        ++self->dispatches;
        if (self->hold_before.exchange(false)) {
            self->held = true;
            until([&] { return self->release.load(); });
        }
        if (self->suppress.load()) std::memset(output, 0, size_t(frames) * CHANNELS * sizeof(float));
        else {
            ++self->actual_calls;
            self->actual_data(device, output, input, frames);
            assert(self->actual_calls <= self->raw_callbacks.size());
            assert(self->raw_count + size_t(frames) * CHANNELS <= self->raw_output.size());
            self->raw_callbacks[self->actual_calls - 1] = {self->raw_count, frames};
            std::memcpy(self->raw_output.data() + self->raw_count, output, size_t(frames) * CHANNELS * sizeof(float));
            self->raw_count += size_t(frames) * CHANNELS;
        }
        const auto *data = static_cast<const float *>(output);
        for (size_t i = 0; i < size_t(frames) * CHANNELS; ++i) if (data[i] != 0.0f) {
            assert(self->capture_count < self->capture.size());
            self->capture[self->capture_count++] = data[i];
        }
        if (self->hold_after.exchange(false)) {
            self->held = true;
            until([&] { return self->release.load(); });
        }
    }
protected:
    int init_device(ma_device *value, const ma_device_config *config) override {
        if (fail_init) return MA_ERROR;
        const auto result = FileAudioSink::init_device(value, config);
        if (result == MA_SUCCESS) {
            device = value; actual_data = value->onData; value->onData = dispatch;
            assert(value->pContext->backend == ma_backend_null);
        }
        return result;
    }
    int start_device(ma_device *value) override {
        if (fail_start_before) return MA_ERROR;
        const auto result = FileAudioSink::start_device(value);
        if (fail_start_after) {
            const auto before = dispatches.load();
            until([&] { return dispatches.load() > before + 2; });
            return MA_ERROR;
        }
        return result;
    }
    int stop_device(ma_device *value) override {
        ++stop_calls;
        const auto result = FileAudioSink::stop_device(value);
        return fail_stop ? MA_ERROR : result;
    }
public:
    ma_device *device = nullptr;
    std::atomic<bool> suppress{false}, hold_before{false}, hold_after{false}, held{false}, release{false};
    std::atomic<uint64_t> dispatches{0};
    bool fail_init = false, fail_start_before = false, fail_start_after = false, fail_stop = false;
    int stop_calls = 0;
    std::array<float, 20000> capture{}; size_t capture_count = 0;
    struct RawCallback { size_t offset = 0; uint32_t frames = 0; };
    std::array<RawCallback, 2048> raw_callbacks{};
    std::vector<float> raw_output = std::vector<float>(1000000);
    size_t raw_count = 0, actual_calls = 0;
    ProbeSink() { assert(!active); active = this; }
    ~ProbeSink() override { release = true; close(); active = nullptr; }
    void observed_dispatches() { auto before = dispatches.load(); until([&] { return dispatches.load() > before + 2; }); }
};
ProbeSink *ProbeSink::active = nullptr;

std::vector<Consumption> drain(const std::shared_ptr<const SinkWatch> &watch) {
    Consumption event; std::vector<Consumption> result;
    while (watch->try_pop_consumption(event)) result.push_back(event);
    return result;
}

std::vector<uint8_t> read_bytes(const std::string &path) {
    std::ifstream input(path, std::ios::binary); assert(input);
    return std::vector<uint8_t>(std::istreambuf_iterator<char>(input), {});
}

void actual_file_vector(const std::string &directory, bool seek) {
    const std::string name = seek ? "seek" : "first";
    const auto wire = read_bytes(directory + "/" + name + ".wire");
    const auto pcm = read_bytes(directory + "/" + name + ".pcm");
    Scope identity; identity.file_session.fill(0x11); identity.transport.fill(0x22); identity.channel.fill(0x33);
    identity.epoch = seek ? 8 : 7; identity.generation = seek ? 10 : 9;
    ProbeSink sink; assert(sink.open(identity)); auto watch = sink.watch();
    Decoder decoder(identity); std::vector<Consumption> events;
    auto collect = [&] { Consumption item; while (watch->try_pop_consumption(item)) events.push_back(item); };
    for (size_t offset=0;offset<wire.size();) {
        const auto size = std::min<size_t>(offset % 2 ? 17 : 4093, wire.size()-offset);
        assert(decoder.feed(wire.data()+offset,size,[&](const Block &value) {
            bool accepted = false;
            until([&] {
                collect(); const auto status = sink.try_enqueue(value);
                assert(status != EnqueueResult::rejected); accepted = status == EnqueueResult::accepted; return accepted;
            });
            return accepted;
        }));
        offset += size;
    }
    assert(decoder.finish());
    until([&]{ collect(); return watch->snapshot().facts().eof_consumed; });
    const auto final = sink.close(); collect(); assert(final.clean_shutdown());
    assert(final.facts().accepted_frames == (seek ? 4043 : 5003));
    assert(final.facts().consumed_frames == final.facts().accepted_frames && final.facts().cancelled_frames == 0);
    assert(final.facts().first_accepted.discontinuity == (seek ? 2 : 1));
    size_t expected_at = 0, callback_at = 0; uint64_t last_callback = 0;
    for (const auto &event : events) {
        assert(event.scope == identity && event.lifetime == final.facts().lifetime);
        assert(event.callback_serial > 0 && event.callback_serial <= sink.actual_calls);
        if (event.callback_serial != last_callback) { assert(event.callback_serial > last_callback); callback_at = 0; last_callback = event.callback_serial; }
        const auto &raw = sink.raw_callbacks[event.callback_serial-1];
        assert(callback_at + event.count <= raw.frames);
        const auto bytes = size_t(event.count) * CHANNELS * sizeof(float);
        assert(expected_at+bytes <= pcm.size());
        assert(std::memcmp(pcm.data()+expected_at, sink.raw_output.data()+raw.offset+callback_at*CHANNELS, bytes)==0);
        expected_at += bytes; callback_at += event.count;
    }
    assert(expected_at==pcm.size() && events.back().begin.eof && events.back().count==0);
    std::printf("PASS actual MediaAudioReader %s vector: %llu frames, original PTS/partial203/EOF, production wire Decoder and exact real Null callback output bytes\n", name.c_str(), (unsigned long long)final.facts().consumed_frames);
}

int main(int argc, char **argv) {
    {
        ProbeSink sink;
        assert(!sink.open(Scope{})); assert(!sink.open(scope(), 4801));
        sink.suppress = true;
        assert(sink.open(scope())); auto watch = sink.watch();
        sink.observed_dispatches();
        assert(!watch->snapshot().component_ready());
        assert(watch->snapshot().facts().callbacks_returned_after_start == 0);
        sink.suppress = false;
        until([&] { return watch->snapshot().component_ready(); });
        assert(watch->snapshot().facts().underflow_frames > 0);
        assert(watch->snapshot().facts().accepted_frames == 0);
        assert(sink.close().clean_shutdown());
        assert(!watch->snapshot().component_ready());
        std::puts("PASS actual start return plus post-return callback; missing callback remains pending; empty queue underflow explicit");
    }
    {
        ProbeSink sink; sink.suppress = true;
        assert(sink.open(scope())); auto watch = sink.watch();
        const auto original = 500000000LL;
        const Block chunks[] = {block(scope(),0,900,original,480,37,137,2),
            block(scope(),1,900,original,480,174,306,2), block(scope(),2,901,original+ns(480),91,0,91),
            eof(scope(),3,902,original+ns(571))};
        for (const auto &chunk : chunks) assert(sink.try_enqueue(chunk) == EnqueueResult::accepted);
        auto accepted = watch->snapshot().facts();
        assert(accepted.first_accepted.offset_samples == 37 && accepted.first_accepted.pts_ns == original);
        assert(accepted.accepted_frames == 534 && accepted.accepted_packets == 4);
        assert(!accepted.eof_consumed);
        sink.suppress = false;
        until([&] { return watch->snapshot().facts().eof_consumed; });
        auto closed = sink.close(); assert(closed.clean_shutdown());
        assert(closed.facts().consumed_frames == 534 && closed.facts().cancelled_frames == 0);
        assert(closed.facts().consumed_packets == 4 && closed.facts().consumed.eof);
        size_t captured = 0; for (const auto &chunk : chunks) for (size_t i = 0; i < size_t(chunk.count)*2; ++i)
            assert(sink.capture[captured++] == chunk.samples[i]);
        assert(captured == sink.capture_count);
        auto events = drain(watch); uint64_t samples = 0, sequence = 0; uint16_t offset = 37;
        for (const auto &event : events) {
            assert(event.scope == scope() && event.lifetime == closed.facts().lifetime && event.callback_serial);
            assert(event.begin.sequence == sequence && event.begin.offset_samples == offset);
            samples += event.count; offset += event.count;
            const auto &chunk = chunks[sequence];
            assert(event.begin.block_id == chunk.block_id && event.begin.pts_ns == chunk.pts_ns);
            if (offset == chunk.offset_samples + chunk.count) {
                ++sequence; if (sequence < 4) offset = chunks[sequence].offset_samples;
            }
        }
        assert(samples == 534 && sequence == 4 && events.back().begin.eof && events.back().count == 0);
        std::puts("PASS actual copied first suffix/slice/partial final block/EOF samples and exact callback consumption ledger");
    }
    {
        ProbeSink sink; sink.suppress = true; assert(sink.open(scope(),480)); auto watch = sink.watch();
        auto first = block(scope(),0,7,0,480,0,480,1);
        assert(sink.try_enqueue(first) == EnqueueResult::accepted);
        auto second = block(scope(),1,8,ns(480),100,0,100);
        auto before = watch->snapshot().facts();
        for (int i=0;i<20;++i) assert(sink.try_enqueue(second) == EnqueueResult::backpressure);
        auto after = watch->snapshot().facts();
        assert(after.accepted_frames == before.accepted_frames && after.accepted_packets == before.accepted_packets);
        assert(after.accepted.sequence == before.accepted.sequence && after.consumed_frames == 0);
        assert(sink.try_enqueue(first) == EnqueueResult::rejected);
        auto invalid = second; invalid.scope = scope(3); assert(sink.try_enqueue(invalid) == EnqueueResult::rejected);
        invalid = second; invalid.sequence = 2; assert(sink.try_enqueue(invalid) == EnqueueResult::rejected);
        invalid = second; ++invalid.block_id; assert(sink.try_enqueue(invalid) == EnqueueResult::rejected);
        invalid = second; invalid.pts_ns += 100; assert(sink.try_enqueue(invalid) == EnqueueResult::rejected);
        invalid = second; invalid.discontinuity = 2; assert(sink.try_enqueue(invalid) == EnqueueResult::rejected);
        sink.suppress = false; until([&] { return watch->snapshot().facts().consumed_frames == 480; });
        assert(sink.try_enqueue(second) == EnqueueResult::accepted);
        assert(sink.try_enqueue(eof(scope(),2,9,ns(580))) == EnqueueResult::accepted);
        until([&] { return watch->snapshot().facts().eof_consumed; });
        assert(sink.close().facts().consumed_frames == 580);
        std::puts("PASS queue-full retry is nonmutating; old scope, duplicate, sequence/block/PTS gap and in-scope seek rejected");
    }
    {
        ProbeSink sink; sink.suppress = true; assert(sink.open(scope())); auto watch = sink.watch();
        std::vector<Block> blocks;
        for (uint64_t i=0;i<10;++i) { blocks.push_back(block(scope(),i,i,ns(i*480),480,0,480)); assert(sink.try_enqueue(blocks.back()) == EnqueueResult::accepted); }
        assert(watch->snapshot().facts().accepted_frames == 4800);
        assert(sink.try_enqueue(block(scope(),10,10,ns(4800),1,0,1)) == EnqueueResult::backpressure);
        assert(sink.try_enqueue(eof(scope(),10,10,ns(4800))) == EnqueueResult::accepted);
        sink.suppress = false; until([&] { return watch->snapshot().facts().eof_consumed; });
        assert(sink.close().facts().consumed_frames == 4800);
        size_t at=0; for (const auto &b : blocks) for (size_t i=0;i<size_t(b.count)*2;++i) assert(sink.capture[at++] == b.samples[i]);
        assert(at == sink.capture_count);
        std::puts("PASS full 100ms queue survives actual playback with no ordinary >40ms discard or partial write acceptance");
    }
    {
        ProbeSink sink; assert(sink.open(scope())); auto watch = sink.watch();
        std::atomic<bool> polling{true};
        std::thread observer([&] {
            while (polling.load()) {
                const auto f = watch->snapshot().facts();
                assert(f.consumed_frames <= f.accepted_frames && f.consumed_packets <= f.accepted_packets);
                assert(f.callbacks_returned_after_start <= f.callbacks_returned && f.callbacks_returned <= f.callbacks_entered);
                assert(f.ledger_read <= f.ledger_written);
                if (f.consumed.valid && !f.consumed.eof) assert(f.consumed_frames == f.consumed.block_id + 1);
            }
        });
        for (uint64_t i=0;i<300;++i) {
            const auto next = block(scope(),i,i,ns(i),1,0,1);
            until([&] { auto result=sink.try_enqueue(next); assert(result != EnqueueResult::rejected); return result==EnqueueResult::accepted; });
        }
        until([&] { return watch->snapshot().facts().ledger_blocked_frames > 0; });
        auto stalled = watch->snapshot().facts();
        assert(stalled.consumed_frames == 256 && stalled.ledger_written == 256 && stalled.accepted_frames == 300);
        sink.observed_dispatches(); assert(watch->snapshot().facts().consumed_frames == 256);
        auto events = drain(watch);
        until([&] { return watch->snapshot().facts().consumed_frames == 300; });
        assert(sink.try_enqueue(eof(scope(),300,300,ns(300))) == EnqueueResult::accepted);
        until([&] { return watch->snapshot().facts().eof_consumed; }); assert(sink.close().clean_shutdown());
        auto remainder = drain(watch); events.insert(events.end(),remainder.begin(),remainder.end());
        assert(events.size()==301);
        for(size_t i=0;i<events.size();++i) { assert(events[i].begin.sequence==i); assert(events[i].count==(i<300?1:0)); }
        assert(sink.capture_count == 600);
        polling=false; observer.join();
        std::puts("PASS bounded ledger backpressure preserves unread evidence and unconsumed PCM; drain resumes every exact sample");
    }
    for (int i=0;i<12;++i) {
        ProbeSink sink; sink.suppress=true; assert(sink.open(scope())); auto old=sink.watch();
        assert(sink.try_enqueue(block(scope(),0,99,0,480,0,480))==EnqueueResult::accepted);
        sink.hold_before=true; sink.suppress=false;
        until([&]{return sink.held.load();});
        std::atomic<bool> returned{false}; SinkSnapshot result;
        std::thread closer([&]{ result=sink.close(); returned=true; });
        until([&]{return old->snapshot().facts().state==SinkState::closing;});
        assert(!returned && !old->snapshot().facts().resources_released);
        sink.release=true; closer.join();
        assert(result.clean_shutdown() && result.facts().cancelled_frames==480 && result.facts().consumed_frames==0);
        const auto old_id=result.facts().lifetime; const auto old_count=old->snapshot().facts().callbacks_returned;
        sink.held=false; sink.release=false; sink.suppress=true;
        assert(sink.open(scope(2))); auto current=sink.watch();
        assert(current->snapshot().facts().lifetime!=old_id);
        assert(!sink.close(old_id).facts().resources_released);
        assert(sink.try_enqueue(block(scope(),0,0,0,1,0,1))==EnqueueResult::rejected);
        assert(old->snapshot().facts().callbacks_returned==old_count && old->snapshot().facts().cancelled_frames==480);
        assert(sink.close().clean_shutdown());
    }
    std::puts("PASS 12 held real callback/close races; actual uninit waits, cancelled frames explicit, stale lifetime cannot close new scope");
    {
        ProbeSink sink; sink.fail_init=true; assert(!sink.open(scope()));
        assert(sink.snapshot().facts().resources_released && !sink.snapshot().clean_shutdown());
        sink.fail_init=false; sink.fail_start_before=true; assert(!sink.open(scope(2)));
        assert(sink.snapshot().facts().device_uninit_returned && !sink.snapshot().component_ready());
        sink.fail_start_before=false; sink.fail_start_after=true; assert(!sink.open(scope(3)));
        assert(sink.snapshot().facts().callbacks_entered>0 && sink.snapshot().facts().callbacks_returned_after_start==0);
        assert(!sink.snapshot().facts().start_returned_success);
        sink.fail_start_after=false; assert(sink.open(scope(4))); auto watch=sink.watch();
        until([&]{return watch->snapshot().component_ready();}); sink.fail_stop=true;
        auto failed=sink.close(); assert(failed.facts().resources_released && !failed.clean_shutdown());
        assert(failed.facts().first_error==MA_ERROR && !watch->snapshot().component_ready());
        int stops=sink.stop_calls; assert(sink.close().facts().first_error==MA_ERROR && sink.stop_calls==stops);
        std::puts("PASS partial init/start failure, callback before failed start return, sticky actual-stop error and duplicate close");
    }
    {
        MiniaudioBackend ordinary; assert(ordinary.initialize(48000,2,240)); auto ordinary_watch=ordinary.startup_watch();
        until([&]{return ordinary_watch->snapshot().ready();});
        ProbeSink sink; assert(sink.open(scope())); auto watch=sink.watch();
        until([&]{return watch->snapshot().component_ready();});
        miniaudio_device_error(sink.device,-77);
        assert(watch->snapshot().facts().first_error==-77 && !watch->snapshot().component_ready());
        assert(ordinary_watch->snapshot().ready());
        assert(sink.close().facts().first_error==-77);
        assert(ordinary.shutdown_observed(ordinary.lifetime()).clean_active_shutdown());
        ma_device unrelated{}; unrelated.pUserData=reinterpret_cast<void*>(1);
        miniaudio_device_error(&unrelated,-88); // Must reject kind before interpreting unknown userdata.
        std::puts("PASS shared typed raw-error dispatch isolates file and ordinary devices and ignores unrelated userdata");
    }
    assert(argc == 2);
    actual_file_vector(argv[1],false);
    actual_file_vector(argv[1],true);
    std::puts("PASS FileAudioSink production/null component; no physical speaker, TLS integration or global FilePCM READY claim");
}
