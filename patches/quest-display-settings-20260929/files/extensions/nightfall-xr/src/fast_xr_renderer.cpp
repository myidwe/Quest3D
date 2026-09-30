#include "fast_xr_renderer.h"

#include <godot_cpp/core/class_db.hpp>

using namespace godot;

NightfallXrRenderer::NightfallXrRenderer() {
	static std::atomic<uint64_t> next_instance{1};
	retirement_lifetime.instance = next_instance.fetch_add(1);
}
NightfallXrRenderer::~NightfallXrRenderer() {
	// OpenXRExtensionWrapper is Object, not RefCounted. No lifetime can be
	// acquired here; ordinary owners must use request_dispose(), not free().
	shutdown_impl();
}

bool NightfallXrRenderer::shutdown() {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	const bool complete = shutdown_impl();
	schedule_shutdown_completion();
	return complete;
}

bool NightfallXrRenderer::request_dispose() {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	dispose_requested = true;
	return shutdown();
}

bool NightfallXrRenderer::is_shutdown_complete() const {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	return shutdown_completed;
}

Dictionary NightfallXrRenderer::get_shutdown_status() const {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	Dictionary result;
	result["requested"] = provider_shutdown_requested;
	result["complete"] = shutdown_completed;
	result["dispose_requested"] = dispose_requested;
	result["disposal_queued"] = disposal_queued;
	result["resource_failure"] = stream_retirement ? int(stream_retirement->failure()) : 0;
	result["provider_unregister_returned"] = provider_shutdown_result && provider_shutdown_result->unregister_call_returned();
	result["provider_api_unavailable"] = provider_shutdown_result && !provider_shutdown_result->api_available();
	return result;
}

void NightfallXrRenderer::schedule_shutdown_completion() {
	if (!dispose_requested || shutdown_completion_queued || disposal_queued ||
			(stream_retirement && !stream_retirement->application_resources_retired()) ||
			(provider_shutdown_result && !provider_shutdown_result->api_available())) return;
	shutdown_completion_queued = true;
	// Object lifetime is explicit, so dropping the scene's Variant does not
	// delete us. Free only in a separate call after provider traversal ends.
	call_deferred("_quest3d_finish_shutdown");
}

void NightfallXrRenderer::_quest3d_finish_shutdown() {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	shutdown_completion_queued = false;
	// Do not reschedule here: Godot drains deferred calls in the same idle
	// cycle. Only an actual later engine notice or explicit request retries.
	if (!dispose_requested || disposal_queued || !shutdown_impl()) return;
	disposal_queued = true;
	emit_signal("shutdown_completed");
	call_deferred("free");
}

XrLifetime NightfallXrRenderer::lifetime() const {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	return retirement_lifetime;
}

std::shared_ptr<const XrRetirementWatch> NightfallXrRenderer::retirement_watch() const {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	return stream_retirement_watch;
}

std::shared_ptr<const XrRetirement> NightfallXrRenderer::stop_stream_observed(XrLifetime expected) {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	if (!(retirement_lifetime == expected)) return nullptr;
	stop_stream();
	return stream_retirement;
}

std::shared_ptr<const XrProviderShutdown> NightfallXrRenderer::provider_shutdown() const {
	std::lock_guard<std::recursive_mutex> lock(frame_mutex);
	return provider_shutdown_result;
}

bool NightfallXrRenderer::begin_stream_lifetime() {
	// A stopped lifetime's failure can never be washed into a new success.
	if ((stream_retirement && !stream_retirement->application_resources_retired()) ||
			(stream_stop_requested && !stream_retirement)) return false;
	++retirement_lifetime.generation;
	stream_retirement.reset();
	stream_retirement_watch.reset(new XrRetirementWatch(retirement_lifetime));
	stream_initialized = false;
	stream_stop_requested = false;
	last_submitted_transport.clear();
#ifdef __ANDROID__
	borrowed_end_failed = false;
	borrowed_end_result = 0;
#endif
	return true;
}

Dictionary NightfallXrRenderer::get_latched_frame_descriptor() const {
    std::lock_guard<std::recursive_mutex> lock(frame_mutex);
    return latched_frame_descriptor.duplicate(true);
}

void NightfallXrRenderer::_bind_methods() {
    ClassDB::bind_method(D_METHOD("get_latched_frame_descriptor"), &NightfallXrRenderer::get_latched_frame_descriptor);
    ClassDB::bind_method(D_METHOD("get_last_successful_submission"), &NightfallXrRenderer::get_last_successful_submission);
    ClassDB::bind_method(D_METHOD("query_pointer_candidate", "origin_world", "direction_world", "max_distance", "eye"), &NightfallXrRenderer::query_pointer_candidate);
    ClassDB::bind_method(D_METHOD("issue_pointer_ticket", "origin_world", "direction_world", "max_distance", "eye"), &NightfallXrRenderer::issue_pointer_ticket);
    ClassDB::bind_method(D_METHOD("take_pointer_ticket", "ticket"), &NightfallXrRenderer::take_pointer_ticket);
    ClassDB::bind_method(D_METHOD("_quest3d_submission_boundary", "notice"), &NightfallXrRenderer::_quest3d_submission_boundary);
	ClassDB::bind_method(D_METHOD("register_provider"), &NightfallXrRenderer::register_provider);
	ClassDB::bind_method(D_METHOD("start", "video_width", "video_height", "full_sbs"), &NightfallXrRenderer::start, DEFVAL(false));
	ClassDB::bind_method(D_METHOD("stop_stream"), &NightfallXrRenderer::stop_stream);
	ClassDB::bind_method(D_METHOD("shutdown"), &NightfallXrRenderer::shutdown);
	ClassDB::bind_method(D_METHOD("request_dispose"), &NightfallXrRenderer::request_dispose);
	ClassDB::bind_method(D_METHOD("is_shutdown_complete"), &NightfallXrRenderer::is_shutdown_complete);
	ClassDB::bind_method(D_METHOD("get_shutdown_status"), &NightfallXrRenderer::get_shutdown_status);
	ClassDB::bind_method(D_METHOD("_quest3d_finish_shutdown"), &NightfallXrRenderer::_quest3d_finish_shutdown);
	ClassDB::add_signal(get_class_static(), MethodInfo("shutdown_completed"));
	ClassDB::bind_method(D_METHOD("stop"), &NightfallXrRenderer::stop);
	ClassDB::bind_method(D_METHOD("is_started"), &NightfallXrRenderer::is_started);
	ClassDB::bind_method(D_METHOD("has_rendered_frame"), &NightfallXrRenderer::has_rendered_frame);
	ClassDB::bind_method(D_METHOD("has_stale_eye_layer"), &NightfallXrRenderer::has_stale_eye_layer);
	ClassDB::bind_method(D_METHOD("consume_pending_resize_failure"), &NightfallXrRenderer::consume_pending_resize_failure);
	ClassDB::bind_method(D_METHOD("supports_cylinder"), &NightfallXrRenderer::supports_cylinder);
	ClassDB::bind_method(D_METHOD("supports_compositor_sharpening"), &NightfallXrRenderer::supports_compositor_sharpening);
	ClassDB::bind_method(D_METHOD("set_geometry", "transform", "width", "height", "curvature", "radius", "central_angle", "sort_order", "bezel_enabled", "crop_top", "crop_bottom"), &NightfallXrRenderer::set_geometry, DEFVAL(0.0), DEFVAL(0.0));
	ClassDB::bind_method(D_METHOD("supports_view_framing"), &NightfallXrRenderer::supports_view_framing);
	ClassDB::bind_method(D_METHOD("set_compositor_sharpening", "mode"), &NightfallXrRenderer::set_compositor_sharpening);
	ClassDB::bind_method(D_METHOD("set_compositor_sampling", "mode", "settings_enabled"), &NightfallXrRenderer::set_compositor_sampling);
	ClassDB::bind_method(D_METHOD("get_compositor_sampling"), &NightfallXrRenderer::get_compositor_sampling);
	ClassDB::bind_method(D_METHOD("submit_frame", "new_frame", "oes_texture_id", "depth_texture_id", "depth_guide_texture_id", "tex_matrix", "distance", "quad_width", "head_locked", "separation", "eye_swap", "passthrough", "oes_fence", "stereo_mode", "depth_revision", "color_transfer_type", "convergence", "brightness", "contrast", "gamma", "frame_source", "picture_color_gain"), &NightfallXrRenderer::submit_frame, DEFVAL(0), DEFVAL(0), DEFVAL(0), DEFVAL(0.5), DEFVAL(0.0), DEFVAL(1.0), DEFVAL(1.0), DEFVAL(Ref<RefCounted>()), DEFVAL(Vector3(1.0, 1.0, 1.0)));
	ClassDB::bind_method(D_METHOD("upload_overlay", "pixels", "width", "height"), &NightfallXrRenderer::upload_overlay);
	ClassDB::bind_method(D_METHOD("set_overlay_visible", "visible"), &NightfallXrRenderer::set_overlay_visible);
	ClassDB::bind_method(D_METHOD("request_ambient_sample"), &NightfallXrRenderer::request_ambient_sample);
	ClassDB::bind_method(D_METHOD("consume_ambient_sample"), &NightfallXrRenderer::consume_ambient_sample);
	ClassDB::bind_method(D_METHOD("get_warp_gpu_ms"), &NightfallXrRenderer::get_warp_gpu_ms);
	ClassDB::bind_method(D_METHOD("set_debug_solid_color", "enabled"), &NightfallXrRenderer::set_debug_solid_color);
}

// Trivial pass-throughs -- see include/fast_xr_renderer.h for why these
// are declared at all.
Dictionary NightfallXrRenderer::_get_requested_extensions(uint64_t p_xr_version) {
	return OpenXRExtensionWrapper::_get_requested_extensions(p_xr_version);
}

uint64_t NightfallXrRenderer::_set_system_properties_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_system_properties_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_instance_create_info_and_get_next_pointer(uint64_t p_xr_version, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_instance_create_info_and_get_next_pointer(p_xr_version, p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_session_create_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_session_create_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_swapchain_create_info_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_swapchain_create_info_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_hand_joint_locations_and_get_next_pointer(int32_t p_hand_index, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_hand_joint_locations_and_get_next_pointer(p_hand_index, p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_projection_views_and_get_next_pointer(int32_t p_view_index, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_projection_views_and_get_next_pointer(p_view_index, p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_frame_wait_info_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_frame_wait_info_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_frame_end_info_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_frame_end_info_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_projection_layer_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_projection_layer_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_view_locate_info_and_get_next_pointer(void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_view_locate_info_and_get_next_pointer(p_next_pointer);
}

uint64_t NightfallXrRenderer::_set_reference_space_create_info_and_get_next_pointer(int32_t p_reference_space_type, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_reference_space_create_info_and_get_next_pointer(p_reference_space_type, p_next_pointer);
}

void NightfallXrRenderer::_prepare_view_configuration(int32_t p_view_count) {
	OpenXRExtensionWrapper::_prepare_view_configuration(p_view_count);
}

uint64_t NightfallXrRenderer::_set_view_configuration_and_get_next_pointer(uint32_t p_view, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_view_configuration_and_get_next_pointer(p_view, p_next_pointer);
}

void NightfallXrRenderer::_print_view_configuration_info(int32_t p_view) const {
	OpenXRExtensionWrapper::_print_view_configuration_info(p_view);
}

PackedStringArray NightfallXrRenderer::_get_suggested_tracker_names() {
	return OpenXRExtensionWrapper::_get_suggested_tracker_names();
}

void NightfallXrRenderer::_on_register_metadata(OpenXRInteractionProfileMetadata *p_interaction_profile_metadata) {
	OpenXRExtensionWrapper::_on_register_metadata(p_interaction_profile_metadata);
}

void NightfallXrRenderer::_on_before_instance_created() {
	OpenXRExtensionWrapper::_on_before_instance_created();
}

void NightfallXrRenderer::_on_instance_created(uint64_t p_instance) {
	OpenXRExtensionWrapper::_on_instance_created(p_instance);
}

void NightfallXrRenderer::_on_instance_destroyed() {
	OpenXRExtensionWrapper::_on_instance_destroyed();
}

void NightfallXrRenderer::_on_session_created(uint64_t p_session) {
	OpenXRExtensionWrapper::_on_session_created(p_session);
}

void NightfallXrRenderer::_on_process() {
	OpenXRExtensionWrapper::_on_process();
}

void NightfallXrRenderer::_on_sync_actions() {
	OpenXRExtensionWrapper::_on_sync_actions();
}

void NightfallXrRenderer::_on_main_swapchains_created() {
	OpenXRExtensionWrapper::_on_main_swapchains_created();
}

void NightfallXrRenderer::_on_pre_draw_viewport(const RID &p_viewport) {
	OpenXRExtensionWrapper::_on_pre_draw_viewport(p_viewport);
}

void NightfallXrRenderer::_on_post_draw_viewport(const RID &p_viewport) {
	OpenXRExtensionWrapper::_on_post_draw_viewport(p_viewport);
}

void NightfallXrRenderer::_on_session_destroyed() {
	OpenXRExtensionWrapper::_on_session_destroyed();
}

void NightfallXrRenderer::_on_state_idle() {
	OpenXRExtensionWrapper::_on_state_idle();
}

void NightfallXrRenderer::_on_state_ready() {
	OpenXRExtensionWrapper::_on_state_ready();
}

void NightfallXrRenderer::_on_state_synchronized() {
	OpenXRExtensionWrapper::_on_state_synchronized();
}

void NightfallXrRenderer::_on_state_visible() {
	OpenXRExtensionWrapper::_on_state_visible();
}

void NightfallXrRenderer::_on_state_focused() {
	OpenXRExtensionWrapper::_on_state_focused();
}

void NightfallXrRenderer::_on_state_stopping() {
	OpenXRExtensionWrapper::_on_state_stopping();
}

void NightfallXrRenderer::_on_state_loss_pending() {
	OpenXRExtensionWrapper::_on_state_loss_pending();
}

void NightfallXrRenderer::_on_state_exiting() {
	OpenXRExtensionWrapper::_on_state_exiting();
}

bool NightfallXrRenderer::_on_event_polled(const void *p_event) {
	return OpenXRExtensionWrapper::_on_event_polled(p_event);
}

uint64_t NightfallXrRenderer::_set_viewport_composition_layer_and_get_next_pointer(const void *p_layer, const Dictionary &p_property_values, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_viewport_composition_layer_and_get_next_pointer(p_layer, p_property_values, p_next_pointer);
}

TypedArray<Dictionary> NightfallXrRenderer::_get_viewport_composition_layer_extension_properties() {
	return OpenXRExtensionWrapper::_get_viewport_composition_layer_extension_properties();
}

Dictionary NightfallXrRenderer::_get_viewport_composition_layer_extension_property_defaults() {
	return OpenXRExtensionWrapper::_get_viewport_composition_layer_extension_property_defaults();
}

void NightfallXrRenderer::_on_viewport_composition_layer_destroyed(const void *p_layer) {
	OpenXRExtensionWrapper::_on_viewport_composition_layer_destroyed(p_layer);
}

uint64_t NightfallXrRenderer::_set_android_surface_swapchain_create_info_and_get_next_pointer(const Dictionary &p_property_values, void *p_next_pointer) {
	return OpenXRExtensionWrapper::_set_android_surface_swapchain_create_info_and_get_next_pointer(p_property_values, p_next_pointer);
}

#ifndef __ANDROID__
// Desktop/editor build: OpenXR-on-GLES swapchain submission is Quest-only.
// The Linux target stays on Godot's existing composition-layer path, so this
// class simply reports unavailable rather than link against EGL/OpenXR.
Dictionary NightfallXrRenderer::get_last_successful_submission() const { return Dictionary(); }
void NightfallXrRenderer::_quest3d_submission_boundary(const Dictionary &) {}
bool NightfallXrRenderer::frame_source_current(const Dictionary &) const { return false; }
void NightfallXrRenderer::invalidate_submission(bool) {}
bool NightfallXrRenderer::register_provider() { return false; }
bool NightfallXrRenderer::start(int, int, bool) { return false; }
void NightfallXrRenderer::stop_stream() {}
bool NightfallXrRenderer::shutdown_impl() { provider_shutdown_requested = true; return shutdown_completed = true; }
void NightfallXrRenderer::stop() {}
bool NightfallXrRenderer::is_started() const { return false; }
bool NightfallXrRenderer::has_rendered_frame() const { return false; }
bool NightfallXrRenderer::has_stale_eye_layer() const { return false; }
bool NightfallXrRenderer::consume_pending_resize_failure() { return false; }
bool NightfallXrRenderer::supports_cylinder() const { return false; }
bool NightfallXrRenderer::supports_compositor_sharpening() const { return false; }
void NightfallXrRenderer::set_geometry(const Transform3D &, float, float, int, float, float, int, bool, float, float) {}
void NightfallXrRenderer::set_compositor_sharpening(int) {}
bool NightfallXrRenderer::set_compositor_sampling(int mode, bool) { return mode == 0; }
int NightfallXrRenderer::get_compositor_sampling() const { return 0; }
bool NightfallXrRenderer::submit_frame(bool, uint32_t, uint32_t, uint32_t, PackedFloat32Array, float, float, bool, float, bool, bool, uint64_t, int, uint64_t, int, float, float, float, float, Ref<RefCounted>, Vector3) { return false; }
void NightfallXrRenderer::upload_overlay(PackedByteArray, int, int) {}
void NightfallXrRenderer::set_overlay_visible(bool) {}
void NightfallXrRenderer::request_ambient_sample() {}
PackedByteArray NightfallXrRenderer::consume_ambient_sample() { return PackedByteArray(); }
float NightfallXrRenderer::get_warp_gpu_ms() { return 0.0f; }
void NightfallXrRenderer::set_debug_solid_color(bool) {}
#endif
