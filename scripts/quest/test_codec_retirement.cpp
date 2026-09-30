#include "video/mediacodec_native.h"
#include <jni.h>
#include <cassert>
#include <chrono>
#include <cstring>
#include <future>
#include <iostream>
#include <type_traits>

using namespace godot;
using namespace std::chrono_literals;
static_assert(!std::is_default_constructible<CodecRetirement>::value);
static_assert(!std::is_default_constructible<CodecRetirementWatch>::value);

JavaVM *nightfall_get_jvm() { return nullptr; }
jclass nightfall_get_godot_app_class() { return nullptr; }

struct AMediaCodec {
    AMediaCodecOnAsyncNotifyCallback cb;
    void *user = nullptr;
    uint8_t input[256]{};
};
struct AImageReader {
    ANativeWindow window;
    AImageReader_ImageListener image;
    AImageReader_BufferRemovedListener removed;
};
namespace {
struct Fixture {
    int callback_error = 0, image_error = 0, buffer_error = 0;
    int stop_error = 0, delete_error = 0, start_error = 0, config_error = 0;
    int deletes = 0, stops = 0, image_deletes = 0, reader_deletes = 0;
    int creates = 0, unregistered = 0, acquired = 0, released = 0;
    bool image_ready = false;
    AMediaCodec *codec = nullptr;
    AImageReader *reader = nullptr;
    std::promise<void> *unregister_event = nullptr;
} f;
void reset() {
    assert(!f.codec && !f.reader);
    f = Fixture{};
}
void output(int64_t pts) {
    auto c = f.codec;
    AMediaCodecBufferInfo info{};
    info.size = 8;
    info.presentationTimeUs = pts;
    c->cb.onAsyncOutputAvailable(c, c->user, 0, &info);
    f.image_ready = true;
}
}
AMediaCodec *AMediaCodec_createDecoderByType(const char *) {
    ++f.creates;
    return f.codec = new AMediaCodec;
}
media_status_t AMediaCodec_delete(AMediaCodec *c) {
    ++f.deletes;
    delete c;
    f.codec = nullptr;
    return f.delete_error;
}
media_status_t AMediaCodec_getName(AMediaCodec *, char **name) {
    *name = ::strdup("fixture.qcom.decoder");
    return 0;
}
void AMediaCodec_releaseName(AMediaCodec *, char *name) { ::free(name); }
media_status_t AMediaCodec_setAsyncNotifyCallback(AMediaCodec *c, AMediaCodecOnAsyncNotifyCallback cb, void *user) {
    c->cb = cb;
    c->user = user;
    if (!user) {
        ++f.unregistered;
        if (f.unregister_event) f.unregister_event->set_value();
        return f.callback_error;
    }
    return 0;
}
media_status_t AMediaCodec_configure(AMediaCodec *, const AMediaFormat *, ANativeWindow *, void *, uint32_t) { return f.config_error; }
media_status_t AMediaCodec_start(AMediaCodec *c) {
    if (!f.start_error) c->cb.onAsyncInputAvailable(c, c->user, 0);
    return f.start_error;
}
media_status_t AMediaCodec_stop(AMediaCodec *) { ++f.stops; return f.stop_error; }
uint8_t *AMediaCodec_getInputBuffer(AMediaCodec *c, size_t, size_t *size) { *size = 256; return c->input; }
media_status_t AMediaCodec_queueInputBuffer(AMediaCodec *, size_t, off_t, size_t, uint64_t, uint32_t) { return 0; }
media_status_t AMediaCodec_releaseOutputBuffer(AMediaCodec *, size_t, bool) { return 0; }
AMediaFormat *AMediaFormat_new() { return new AMediaFormat; }
void AMediaFormat_delete(AMediaFormat *format) { delete format; }
void AMediaFormat_setString(AMediaFormat *, const char *, const char *) {}
void AMediaFormat_setInt32(AMediaFormat *format, const char *key, int32_t value) {
    if (!std::strcmp(key, "width")) format->width = value;
    if (!std::strcmp(key, "height")) format->height = value;
}
bool AMediaFormat_getInt32(AMediaFormat *format, const char *key, int32_t *value) {
    if (!std::strcmp(key, "width")) { *value = format->width; return format->width_present; }
    if (!std::strcmp(key, "height")) { *value = format->height; return format->height_present; }
    const char *crop_keys[] = {"crop-left", "crop-top", "crop-right", "crop-bottom"};
    for (int i = 0; i < 4; ++i) {
        if (!std::strcmp(key, crop_keys[i]) && (format->crop_mask & (1u << i))) {
            *value = format->crop[i]; return true;
        }
    }
    return false;
}
bool AMediaFormat_getRect(AMediaFormat *format, const char *key, int32_t *left,
                         int32_t *top, int32_t *right, int32_t *bottom) {
    if (std::strcmp(key, "crop") || !format->crop_rectangle) return false;
    *left = format->crop[0]; *top = format->crop[1];
    *right = format->crop[2]; *bottom = format->crop[3];
    return true;
}
media_status_t AImageReader_newWithUsage(int32_t, int32_t, int32_t, uint64_t, int32_t, AImageReader **reader) {
    *reader = f.reader = new AImageReader;
    return 0;
}
media_status_t AImageReader_setImageListener(AImageReader *r, AImageReader_ImageListener *l) {
    r->image = l ? *l : AImageReader_ImageListener{};
    return l ? 0 : f.image_error;
}
media_status_t AImageReader_setBufferRemovedListener(AImageReader *r, AImageReader_BufferRemovedListener *l) {
    r->removed = l ? *l : AImageReader_BufferRemovedListener{};
    return l ? 0 : f.buffer_error;
}
media_status_t AImageReader_getWindow(AImageReader *r, ANativeWindow **window) { *window = &r->window; return 0; }
media_status_t AImageReader_acquireLatestImage(AImageReader *, AImage **image) {
    if (!f.image_ready) return AMEDIA_IMGREADER_NO_BUFFER_AVAILABLE;
    *image = new AImage;
    f.image_ready = false;
    return 0;
}
void AImageReader_delete(AImageReader *r) { ++f.reader_deletes; delete r; f.reader = nullptr; }
media_status_t AImage_getHardwareBuffer(const AImage *image, AHardwareBuffer **buffer) {
    *buffer = &const_cast<AImage *>(image)->buffer;
    return 0;
}
// Explicit AHB references survive AImage_delete in the real API. Keep the
// fixture image allocation until that last explicit reference is released.
void AImage_delete(AImage *image) {
    ++f.image_deletes;
    if (--image->buffer.refs == 0) delete image;
}
void AHardwareBuffer_acquire(AHardwareBuffer *b) { ++b->refs; }
void AHardwareBuffer_release(AHardwareBuffer *b) {
    if (--b->refs == 0) delete reinterpret_cast<AImage *>(b);
}
void ANativeWindow_acquire(ANativeWindow *w) { ++f.acquired; ++w->refs; }
void ANativeWindow_release(ANativeWindow *w) { ++f.released; --w->refs; }

int main() {
    int cases = 0;
    {
        reset();
        AndroidMediaCodec c;
        assert(!c.output_format().observed);
        assert(c.init("video/avc", 2560, 720, 30, false));
        assert(!c.output_format().observed); // init/setup is not an output event.
        auto native = f.codec;
        AMediaFormat format;
        format.width = 3200; format.height = 904;
        auto publish = [&] { native->cb.onAsyncFormatChanged(native, native->user, &format); };
        publish();
        const auto first = c.output_format();
        assert(first.observed && first.valid && !first.crop_present);
        assert(first.lifetime == c.lifetime() && first.width == 3200 && first.height == 904);
        assert(first.crop_right == 3199 && first.crop_bottom == 903);
        format.crop_mask = 15;
        format.crop[0] = 0; format.crop[1] = 2;
        format.crop[2] = 3199; format.crop[3] = 901;
        publish();
        assert(c.output_format().valid && c.output_format().crop_present);
        assert(c.output_format().crop_bottom - c.output_format().crop_top + 1 == 900);
        const auto cropped = c.output_format();
        format.crop_mask = 0;
        format.crop_rectangle = true;
        publish();
        assert(c.output_format().valid && c.output_format().crop_bottom == 901);
        format.crop_rectangle = false;
        format.crop_mask = 15;
        // Reject malformed newer output instead of retaining the old format.
        for (int fault = 0; fault < 7; ++fault) {
            AMediaFormat bad = format;
            if (fault == 0) bad.width = 0;
            if (fault == 1) bad.height = -1;
            if (fault == 2) bad.width_present = false;
            if (fault == 3) bad.crop_mask = 7;
            if (fault == 4) bad.crop[0] = -1;
            if (fault == 5) bad.crop[2] = bad.width;
            if (fault == 6) bad.crop[3] = 1;
            native->cb.onAsyncFormatChanged(native, native->user, &bad);
            assert(c.output_format().observed && !c.output_format().valid);
            publish();
            assert(c.output_format().valid);
        }
        AMediaCodec foreign;
        AMediaFormat wrong;
        wrong.width = 64; wrong.height = 64;
        native->cb.onAsyncFormatChanged(&foreign, native->user, &wrong);
        assert(c.output_format().width == 3200);
        // Concurrent reads see one callback's coherent crop/dimensions.
        auto reader = std::async(std::launch::async, [&] {
            for (int i = 0; i < 1000; ++i) {
                const auto observed = c.output_format();
                assert(observed.valid && observed.lifetime == cropped.lifetime);
                assert((observed.width == 3200 && observed.crop_right == 3199) ||
                       (observed.width == 3840 && observed.crop_right == 3839));
            }
        });
        for (int i = 0; i < 1000; ++i) {
            format.width = i % 2 ? 3200 : 3840;
            format.crop[2] = format.width - 1;
            publish();
        }
        reader.get();
        c.shutdown();
        assert(!c.output_format().observed);
        assert(c.init("video/avc", 3840, 1080, 30, false));
        assert(!(c.lifetime() == first.lifetime) && !c.output_format().observed);
        assert(cropped.valid && cropped.width == 3200); // immutable value copy.
        c.shutdown();
        f.start_error = -21;
        assert(!c.init("video/avc", 3840, 1080, 30, false));
        assert(!c.output_format().observed);
        cases += 16;
    }
    {
        AndroidMediaCodec c;
        assert(!c.shutdown_observed(c.lifetime()));
        assert(!c.retirement(c.lifetime()));
        ++cases;
    }
    {
        reset();
        AndroidMediaCodec c, other;
        ANativeWindow window;
        assert(c.init("video/avc", 1280, 720, 30, false, &window));
        auto first = c.lifetime();
        auto first_watch = c.retirement_watch();
        assert(first_watch && !first_watch->snapshot());
        assert(!c.shutdown_observed(other.lifetime()));
        assert(c.is_initialized());
        auto result = c.shutdown_observed(first);
        assert(result && result->initialized_codec_retired());
        assert(first_watch->snapshot() == result);
        assert(f.stops == 1 && f.deletes == 1 && f.acquired == 1 && f.released == 1 && window.refs == 1);
        assert(c.shutdown_observed(first) == result && f.deletes == 1);
        assert(c.init("video/avc", 1280, 720, 30, false, &window));
        assert(!c.shutdown_observed(first) && !c.retirement(first));
        assert(c.is_initialized() && result->lifetime() == first);
        assert(result->initialized_codec_retired());
        assert(first_watch->snapshot() == result && c.retirement_watch() != first_watch);
        c.shutdown();
        assert(c.retirement(c.lifetime())->initialized_codec_retired());
        cases += 4;
    }
    {
        reset();
        AndroidMediaCodec c;
        assert(c.init("video/avc", 1280, 720, 30, false));
        uint8_t data[] = {1, 2, 3};
        assert(c.feed_packet(data, sizeof(data), 300) == AndroidMediaCodec::FeedResult::QUEUED);
        output(300);
        NativeDecodedFrame frame;
        assert(c.dequeue_frame(frame, 0) && frame.image && frame.buffer && frame.pts == 300);
        c.release_frame(frame);
        c.release_frame(frame); // Same cleared object, not a copied ownership alias.
        auto result = c.shutdown_observed(c.lifetime());
        assert(result->initialized_codec_retired() && f.image_deletes == 1 && f.reader_deletes == 1);
        ++cases;
    }
    {
        reset();
        auto c = std::make_shared<AndroidMediaCodec>();
        assert(c->init("video/avc", 1280, 720, 30, false));
        auto watch = c->retirement_watch();
        assert(watch && !watch->snapshot());
        c.reset();
        assert(watch->snapshot() && watch->snapshot()->initialized_codec_retired());
        assert(f.deletes == 1 && f.reader_deletes == 1);
        ++cases;
    }
    for (int error = 0; error < 5; ++error) {
        reset();
        AndroidMediaCodec c;
        assert(c.init("video/avc", 1280, 720, 30, false));
        if (error == 0) f.callback_error = -11;
        if (error == 1) f.image_error = -12;
        if (error == 2) f.buffer_error = -13;
        if (error == 3) f.stop_error = -14;
        if (error == 4) f.delete_error = -15;
        const auto id = c.lifetime();
        auto result = c.shutdown_observed(id);
        assert(result && result->initialized() && !result->resources_released());
        assert(f.deletes == 1 && f.reader_deletes == 1);
        assert(c.shutdown_observed(id) == result && f.deletes == 1);
        assert(!c.init("video/avc", 1280, 720, 30, false));
        assert(c.lifetime() == id && c.retirement(id) == result);
        ++cases;
    }
    {
        reset();
        AndroidMediaCodec c;
        f.start_error = -21;
        assert(!c.init("video/avc", 1280, 720, 30, false));
        auto result = c.retirement(c.lifetime());
        assert(result && !result->initialized() && result->resources_released());
        assert(!result->initialized_codec_retired() && f.deletes == 1);
        f.start_error = 0;
        assert(c.init("video/avc", 1280, 720, 30, false));
        c.shutdown();
        assert(c.retirement(c.lifetime())->initialized_codec_retired());
        ++cases;
    }
    {
        reset();
        AndroidMediaCodec c;
        f.config_error = -22;
        f.delete_error = -23;
        assert(!c.init("video/avc", 1280, 720, 30, false));
        auto result = c.retirement(c.lifetime());
        assert(result && result->prior_delete_error() == -23 && !result->resources_released());
        assert(f.creates == 1 && f.deletes == 1);
        ++cases;
    }
    {
        reset();
        AndroidMediaCodec c;
        assert(c.init("video/avc", 1280, 720, 30, false));
        output(400);
        NativeDecodedFrame frame;
        assert(c.dequeue_frame(frame, 0));
        auto result = c.shutdown_observed(c.lifetime());
        assert(result && result->outstanding_images() == 1 && !result->resources_released());
        // Deliberate protocol violation uses the fixture's retained allocation;
        // doing this after real AImageReader_delete is not a supported caller.
        c.release_frame(frame);
        assert(c.shutdown_observed(c.lifetime()) == result && !result->resources_released());
        ++cases;
    }
    {
        reset();
        AndroidMediaCodec c;
        std::promise<void> entered, release, unregistered;
        auto release_future = release.get_future().share();
        auto entered_future = entered.get_future();
        auto unregistered_future = unregistered.get_future();
        std::atomic<bool> hold_next{false};
        assert(c.init("video/avc", 1280, 720, 30, false, nullptr, [&] {
            if (hold_next.exchange(false)) {
                entered.set_value();
                release_future.wait();
            }
        }));
        auto native = f.codec;
        const auto cb = native->cb;
        void *user = native->user;
        hold_next = true;
        std::thread callback([&] { cb.onAsyncInputAvailable(native, user, 1); });
        assert(entered_future.wait_for(2s) == std::future_status::ready);
        f.unregister_event = &unregistered;
        auto completion = std::async(std::launch::async, [&] { return c.shutdown_observed(c.lifetime()); });
        assert(unregistered_future.wait_for(2s) == std::future_status::ready);
        assert(completion.wait_for(10ms) == std::future_status::timeout);
        release.set_value();
        callback.join();
        assert(completion.get()->initialized_codec_retired());
        f.unregister_event = nullptr;
        ++cases;
    }
    reset();
    std::cout << "Production AndroidMediaCodec lifecycle: " << cases
              << " host NDK-fixture cases passed; Android hardware not tested\n";
}
