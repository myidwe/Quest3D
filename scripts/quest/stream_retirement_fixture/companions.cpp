// Non-target graphics/input/depth classes are explicit inert fixture companions.
// No graphics operation or FFmpeg decoder is exercised by this CPU lifecycle test.
#include "video/ffmpeg_decoder.h"
#include "video/texture_uploader.h"
#include "video/depth_bridge.h"
#include "input/input_bridge.h"
#include "nightfall_stream.h"
#include "video/pipewire_capture.h"
#include "video/dmabuf_importer.h"
#include "video/x11_capture.h"
#include "audio/pipewire_audio.h"
extern "C" {
#include <Limelight.h>
}
using namespace godot;
FfmpegDecoder::FfmpegDecoder()=default;
FfmpegDecoder::~FfmpegDecoder()=default;
void FfmpegDecoder::_bind_methods() {}
int FfmpegDecoder::probe_video_format(int,bool) { return VIDEO_FORMAT_MASK_H264; }
int FfmpegDecoder::setup(int,int,int,bool) { return 0; }
void FfmpegDecoder::cleanup() {}
String FfmpegDecoder::get_decoder_name() const { return "fixture"; }
bool FfmpegDecoder::is_hw_decode() const { return false; }
bool FfmpegDecoder::is_raw_decode() const { return false; }
int FfmpegDecoder::get_video_width() const { return 0; }
int FfmpegDecoder::get_video_height() const { return 0; }
int FfmpegDecoder::upgrade_to_mediacodec(const uint8_t*,int) { return -1; }
TextureUploader::TextureUploader()=default;
TextureUploader::~TextureUploader()=default;
void TextureUploader::_bind_methods() { ADD_SIGNAL(MethodInfo("surface_failed")); }
void TextureUploader::setup(int,int,int,int,int,int) {}
void TextureUploader::ensure_shader_material() {}
void TextureUploader::set_active(bool) {}
void TextureUploader::set_texture_from_native_rid(RID,int,int) {}
void TextureUploader::cleanup() {}
void TextureUploader::update_from_frame(AVFrame*) {}
void TextureUploader::update_from_raw_nv12(int,int,const uint8_t*,uint32_t,uint32_t) {}
void TextureUploader::update_colorspace(int,int,int) {}
void TextureUploader::update_color_transfer(int) {}
DepthBridge::DepthBridge()=default;
DepthBridge::~DepthBridge()=default;
void DepthBridge::_bind_methods() {}
InputBridge::InputBridge()=default;
InputBridge::~InputBridge()=default;
void InputBridge::_bind_methods() {}

// Config/network/capture are inert only in this isolated wrapper fixture.
// NightfallStream and its Timer/state/signal/retirement code are production.
NightfallConfigManager::NightfallConfigManager()=default;
NightfallConfigManager::~NightfallConfigManager()=default;
void NightfallConfigManager::_bind_methods() {}
NightfallComputerManager::NightfallComputerManager()=default;
NightfallComputerManager::~NightfallComputerManager()=default;
void NightfallComputerManager::_bind_methods() {}
void NightfallComputerManager::set_config_manager(Object*) {}
void NightfallComputerManager::set_http_requester(Object*) {}
void NightfallComputerManager::set_parent_node(Node*) {}
HttpRequester::HttpRequester()=default;
HttpRequester::~HttpRequester()=default;
void HttpRequester::_bind_methods() {}
PipeWireCapture::PipeWireCapture()=default;
PipeWireCapture::~PipeWireCapture()=default;
bool PipeWireCapture::start(const std::string&) { return false; }
void PipeWireCapture::stop() {}
bool PipeWireCapture::has_new_frame() { return false; }
bool PipeWireCapture::get_latest_frame(FrameData&) { return false; }
void PipeWireCapture::release_frame(void*) {}
DmaBufImporter::DmaBufImporter(Ref<TextureUploader>) {}
DmaBufImporter::~DmaBufImporter()=default;
bool DmaBufImporter::import_frame(const PipeWireCapture::FrameData&) { return false; }
PipeWireAudio::PipeWireAudio(AudioRenderer*) {}
PipeWireAudio::~PipeWireAudio()=default;
bool PipeWireAudio::start() { return false; }
void PipeWireAudio::stop() {}
X11Capture::X11Capture()=default;
X11Capture::~X11Capture()=default;
bool X11Capture::start() { return false; }
void X11Capture::stop() {}
bool X11Capture::has_new_frame() const { return false; }
bool X11Capture::get_latest_frame(FrameData&) { return false; }
void X11Capture::release_frame() {}
void TextureUploader::setup_bgra(int,int) {}
void TextureUploader::update_from_raw_bgra(int,int,const uint8_t*,uint32_t) {}
