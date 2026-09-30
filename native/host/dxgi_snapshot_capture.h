#pragma once
#include <stdint.h>

#ifdef _WIN32
#ifdef Q3D_DXGI_BUILD
#define Q3D_DXGI_API __declspec(dllexport)
#else
#define Q3D_DXGI_API __declspec(dllimport)
#endif
#else
#define Q3D_DXGI_API
#endif
#ifdef __cplusplus
extern "C" {
#endif

enum q3d_dxgi_status {
  Q3D_DXGI_OK = 0,
  Q3D_DXGI_INVALID_ARGUMENT = 1,
  Q3D_DXGI_INVALID_HANDLE = 2,
  Q3D_DXGI_WRONG_THREAD = 3,
  Q3D_DXGI_CAPACITY = 4,
  Q3D_DXGI_TIMEOUT = 5,
  Q3D_DXGI_ACCESS_DENIED = 6,
  Q3D_DXGI_SOURCE_CHANGED = 7,
  Q3D_DXGI_DEVICE_LOST = 8,
  Q3D_DXGI_STALE_FRAME = 9,
  Q3D_DXGI_UNSUPPORTED = 10,
  Q3D_DXGI_OS_ERROR = 11,
  Q3D_DXGI_INTERNAL_ERROR = 12,
  Q3D_DXGI_OWNER_DEAD = 13
};
enum q3d_dxgi_stage {
  Q3D_STAGE_ARGUMENT = 1,
  Q3D_STAGE_SOURCE = 2,
  Q3D_STAGE_DEVICE = 3,
  Q3D_STAGE_DUPLICATE = 4,
  Q3D_STAGE_ACQUIRE = 5,
  Q3D_STAGE_TEXTURE = 6,
  Q3D_STAGE_CAPACITY = 7,
  Q3D_STAGE_MAP = 8,
  Q3D_STAGE_COPY = 9,
  Q3D_STAGE_RELEASE = 10,
  Q3D_STAGE_CLOSE = 11
};

// ABI v1, native little endian, natural Windows x64 alignment. No pointers in
// results. Thread DPI must already be PER_MONITOR(_V2). Only identity rotation.
typedef struct q3d_dxgi_source {
  uint32_t version, size;
  uint64_t hmonitor, adapter_luid;
  int32_t left, top;
  uint32_t width, height, rotation, reserved;
  uint16_t device_name[32];
} q3d_dxgi_source;

typedef struct q3d_dxgi_frame {
  uint32_t version, size;
  q3d_dxgi_source source;
  uint32_t format, row_bytes, rows, bytes_per_pixel;
  uint64_t copied_bytes, frame_id;
  uint64_t last_present_qpc, acquire_started_qpc, acquire_returned_qpc;
  uint64_t map_completed_qpc, copy_completed_qpc, qpc_frequency;
  uint64_t last_mouse_qpc;
  uint32_t accumulated_frames, protected_content_masked;
  uint64_t reserved;
} q3d_dxgi_frame;

typedef struct q3d_dxgi_error {
  uint32_t version, size;
  int32_t status, hresult;
  uint64_t required_capacity;
  uint32_t stage, reserved;
} q3d_dxgi_error;

// Open does not acquire pixels. A caller may allocate width*height*8 bytes from
// source; snapshot reports the actual format and packed stride. All calls for
// a handle must be on its opening thread. Handles never reuse. Close alone may
// reap an abandoned session after the real opening-thread HANDLE is signalled;
// a recycled thread ID can never authorize a snapshot.
Q3D_DXGI_API int32_t q3d_dxgi_open(uint64_t hmonitor, uint64_t *handle,
                                   q3d_dxgi_source *source,
                                   uint32_t source_size, q3d_dxgi_error *error);
// Capacity is checked before writing. Caller memory must be valid and remain
// exclusively owned throughout the call. No borrowed D3D resource escapes.
// timeout_ms:1..1000, max_age_ms:1..500. SUCCESS requires a nonzero, nonfuture
// OS LastPresentTime still within max_age after CPU copy and source validation.
// Metadata is zero on failure. If failure follows copying, those copied bytes
// are zeroed; otherwise caller pixels are untouched and must still be ignored.
Q3D_DXGI_API int32_t q3d_dxgi_snapshot(uint64_t handle, void *pixels,
                                       uint64_t capacity, uint32_t timeout_ms,
                                       uint32_t max_age_ms,
                                       q3d_dxgi_frame *frame,
                                       uint32_t frame_size,
                                       q3d_dxgi_error *error);
Q3D_DXGI_API int32_t q3d_dxgi_close(uint64_t handle, q3d_dxgi_error *error);

#ifdef __cplusplus
}
static_assert(sizeof(q3d_dxgi_source) == 112);
static_assert(sizeof(q3d_dxgi_frame) == 224);
static_assert(sizeof(q3d_dxgi_error) == 32);
#endif
