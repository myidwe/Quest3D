#pragma once
// Host-only fault fixture. These APIs model NDK calls; they do not decode video
// or exercise an Android driver. The production AndroidMediaCodec is compiled
// unchanged against this surface by test_codec_retirement.sh.
#include <cstddef>
#include <cstdint>
#include <sys/types.h>

using media_status_t = int32_t;
constexpr media_status_t AMEDIA_OK = 0;
constexpr media_status_t AMEDIA_IMGREADER_NO_BUFFER_AVAILABLE = -10001;
constexpr uint32_t AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM = 4;
constexpr uint64_t AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN = 3;
constexpr uint64_t AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE = 256;
constexpr int AIMAGE_FORMAT_RGBA_8888 = 1, AIMAGE_FORMAT_YUV_420_888 = 35;
constexpr auto AMEDIAFORMAT_KEY_MIME = "mime";
constexpr auto AMEDIAFORMAT_KEY_WIDTH = "width";
constexpr auto AMEDIAFORMAT_KEY_HEIGHT = "height";
constexpr auto AMEDIAFORMAT_KEY_MAX_INPUT_SIZE = "max-input-size";
struct AMediaCodec;
struct AMediaFormat {
    int width = 0, height = 0;
    bool width_present = true, height_present = true;
    int crop[4]{};
    unsigned crop_mask = 0;
    bool crop_rectangle = false;
};
struct ANativeWindow { int refs = 1; };
struct AHardwareBuffer { int refs = 1; };
struct AImage { AHardwareBuffer buffer; };
struct AImageReader;
struct AMediaCodecBufferInfo {
    int32_t offset = 0, size = 0;
    int64_t presentationTimeUs = 0;
    uint32_t flags = 0;
};
struct AMediaCodecOnAsyncNotifyCallback {
    void (*onAsyncInputAvailable)(AMediaCodec *, void *, int32_t) = nullptr;
    void (*onAsyncOutputAvailable)(AMediaCodec *, void *, int32_t, AMediaCodecBufferInfo *) = nullptr;
    void (*onAsyncFormatChanged)(AMediaCodec *, void *, AMediaFormat *) = nullptr;
    void (*onAsyncError)(AMediaCodec *, void *, media_status_t, int32_t, const char *) = nullptr;
};
struct AImageReader_ImageListener {
    void *context = nullptr;
    void (*onImageAvailable)(void *, AImageReader *) = nullptr;
};
struct AImageReader_BufferRemovedListener {
    void *context = nullptr;
    void (*onBufferRemoved)(void *, AImageReader *, AHardwareBuffer *) = nullptr;
};
AMediaCodec *AMediaCodec_createDecoderByType(const char *);
media_status_t AMediaCodec_delete(AMediaCodec *);
media_status_t AMediaCodec_getName(AMediaCodec *, char **);
void AMediaCodec_releaseName(AMediaCodec *, char *);
media_status_t AMediaCodec_setAsyncNotifyCallback(AMediaCodec *, AMediaCodecOnAsyncNotifyCallback, void *);
media_status_t AMediaCodec_configure(AMediaCodec *, const AMediaFormat *, ANativeWindow *, void *, uint32_t);
media_status_t AMediaCodec_start(AMediaCodec *);
media_status_t AMediaCodec_stop(AMediaCodec *);
uint8_t *AMediaCodec_getInputBuffer(AMediaCodec *, size_t, size_t *);
media_status_t AMediaCodec_queueInputBuffer(AMediaCodec *, size_t, off_t, size_t, uint64_t, uint32_t);
media_status_t AMediaCodec_releaseOutputBuffer(AMediaCodec *, size_t, bool);
AMediaFormat *AMediaFormat_new();
void AMediaFormat_delete(AMediaFormat *);
void AMediaFormat_setString(AMediaFormat *, const char *, const char *);
void AMediaFormat_setInt32(AMediaFormat *, const char *, int32_t);
bool AMediaFormat_getInt32(AMediaFormat *, const char *, int32_t *);
bool AMediaFormat_getRect(AMediaFormat *, const char *, int32_t *, int32_t *, int32_t *, int32_t *);
media_status_t AImageReader_newWithUsage(int32_t, int32_t, int32_t, uint64_t, int32_t, AImageReader **);
media_status_t AImageReader_setImageListener(AImageReader *, AImageReader_ImageListener *);
media_status_t AImageReader_setBufferRemovedListener(AImageReader *, AImageReader_BufferRemovedListener *);
media_status_t AImageReader_getWindow(AImageReader *, ANativeWindow **);
media_status_t AImageReader_acquireLatestImage(AImageReader *, AImage **);
void AImageReader_delete(AImageReader *);
media_status_t AImage_getHardwareBuffer(const AImage *, AHardwareBuffer **);
void AImage_delete(AImage *);
void AHardwareBuffer_acquire(AHardwareBuffer *);
void AHardwareBuffer_release(AHardwareBuffer *);
void ANativeWindow_acquire(ANativeWindow *);
void ANativeWindow_release(ANativeWindow *);
