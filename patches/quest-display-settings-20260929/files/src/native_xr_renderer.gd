class_name NativeXrRendererManager
extends RefCounted

# Production bridge for the Android/GLES composition-layer renderer. The
# legacy Godot viewport path remains live as a fallback and for multi-monitor
# and depth diagnostic modes.

var main: Node3D
var renderer = null
var provider_registered := false
var active := false
var stream_started := false
var legacy_disabled := false
var failure_reason := ""
var _last_size := Vector2i.ZERO
var _last_full_sbs := false
var _last_mode := -1
var _last_eligible := false
var _stats_upload_delay := -1
var _stale_recovery_until_msec := 0
var _shutdown_complete := false
var _shutdown_requested := false
var _picture_refresh_pending := false
const PcColorProbe = preload("res://src/pc_color_probe.gd")
var _color_probe: RefCounted
var _color_probe_poll_ms := -1000
var _color_probe_request_hash := ""
const COLOR_PROBE_REQUEST := "user://quest3d-color-probe.request.json"
const COLOR_PROBE_STATUS := "user://quest3d-color-probe.status.json"

const STALE_EYE_RECOVERY_MSEC := 2000

func _init(owner: Node3D) -> void:
	main = owner
	_color_probe = PcColorProbe.new(OS.is_debug_build(), main.PC_SBS_BUILD, OS.get_cmdline_user_args())

func setup() -> void:
	if _shutdown_requested:
		return
	if OS.get_name() != "Android":
		failure_reason = "non-Android platform"
		return
	if RenderingServer.get_current_rendering_method() != "gl_compatibility":
		failure_reason = "renderer is not GLES"
		return
	if "--nf-legacy-video" in OS.get_cmdline_user_args():
		failure_reason = "legacy override"
		return
	if not ClassDB.class_exists("NightfallXrRenderer"):
		failure_reason = "native extension unavailable"
		return
	renderer = ClassDB.instantiate("NightfallXrRenderer")
	if renderer == null or not renderer.register_provider():
		if is_instance_valid(renderer):
			renderer.request_dispose()
		renderer = null
		failure_reason = "composition provider registration failed"
		return
	provider_registered = true
	renderer.shutdown_completed.connect(_on_native_shutdown_completed)
	main._log("[NATIVE-XR] Composition provider registered")

func _mode() -> int:
	return main.settings_controller.get_stereo_mode() if main.settings_controller else 0

func _native_compositor_sharpening() -> int:
	if main.settings.sharpen_mode == main.SHARPEN_RUNTIME_NORMAL:
		return 1
	if main.settings.sharpen_mode == main.SHARPEN_RUNTIME_QUALITY:
		return 2
	return 0

func _engine_compositor_filter_enabled() -> bool:
	# Patched engine accessor reads the bool written during actual instance creation.
	# Availability enumeration by itself is not sufficient for the new candidate.
	if not main.primary_screen:
		return false
	for layer in [main.primary_screen.comp_cylinder, main.primary_screen.comp_cylinder_left, main.primary_screen.comp_cylinder_right]:
		if is_instance_valid(layer) and layer.has_method("is_compositor_filter_supported") and layer.is_compositor_filter_supported():
			return true
	return false

func video_sampling_supported() -> bool:
	return active and not _shutdown_requested and provider_registered and is_instance_valid(renderer) \
		and renderer.has_method("set_compositor_sampling") and renderer.supports_compositor_sharpening() \
		and _engine_compositor_filter_enabled()

func apply_video_sampling(mode: int) -> bool:
	if mode != 0 and (mode != 1 or not video_sampling_supported()):
		return false
	if not is_instance_valid(renderer) or not renderer.has_method("set_compositor_sampling"):
		return mode == 0
	return renderer.set_compositor_sampling(mode, _engine_compositor_filter_enabled())

func _sync_video_sampling() -> void:
	if not main.settings_controller:
		return
	var mode: int = main.settings_controller.video_sampling_mode
	if not apply_video_sampling(mode) and mode != 0:
		# Capability loss never leaves an unsupported bit attached to later frames.
		apply_video_sampling(0)
		main.settings_controller.video_sampling_mode = 0
		main._log("[SAMPLING] Native capability unavailable; restored Current")
	main.settings_controller.refresh_video_sampling_controls()

func can_render_current_config() -> bool:
	if _shutdown_requested or not provider_registered or not is_instance_valid(renderer) or main.screens.size() != 1:
		return false
	# Auto-detection and shader-based sharpening consume the legacy RGB
	# viewport. Runtime compositor sharpening is attached directly to our own
	# OpenXR layers and therefore remains eligible for the fast path.
	if main.auto_detect_enabled:
		return false
	var native_sharpening := _native_compositor_sharpening()
	if main.settings.sharpen_mode != 0 and native_sharpening == 0:
		return false
	if native_sharpening > 0 and not renderer.supports_compositor_sharpening():
		return false
	var mode := _mode()
	if mode >= 7 and mode <= 9:
		return false
	if main.primary_screen and main.primary_screen.curvature > 0 and not renderer.supports_cylinder():
		return false
	return true

func _eligible() -> bool:
	if not main.is_streaming or not can_render_current_config():
		return false
	# _schedule_stream_restart() deactivates this renderer, then awaits a
	# couple of frame boundaries before actually freeing the decoder/texture
	# state via stop_play_stream(). process_frame() -> refresh() runs every
	# frame regardless, so without this guard _eligible() keeps returning
	# true through those awaits and re-activates the renderer (renderer.start())
	# right as the decoder/OES state it reads is about to be torn down -
	# a null-pointer GLThread crash inside NightfallStream::stop_stream()'s
	# call chain, confirmed via adb logcat crash buffer (SIGSEGV, fault
	# addr 0xe0, repeated across a test session with heavy setting toggling).
	if main.session_lifecycle.is_restarting():
		return false
	# A per-eye freeze -- stale pose/subImage resubmitted at the OpenXR
	# layer-collection level while the other eye keeps updating normally --
	# has been observed on-device, triggered by head rotation and unrelated
	# to any setting change (see has_stale_eye_layer()'s C++ comment for the
	# detection mechanism). The legacy renderer doesn't submit per-eye
	# composition layers, so it isn't subject to this. Rather than leave the
	# affected eye stuck indefinitely, fall back to legacy for a cooldown
	# window and then let this renderer resume automatically -- a transient
	# outage instead of a stuck frame.
	if active and renderer.has_stale_eye_layer():
		_stale_recovery_until_msec = Time.get_ticks_msec() + STALE_EYE_RECOVERY_MSEC
		main._log("[NATIVE-XR] Stale eye layer detected; falling back to legacy renderer for %dms" % STALE_EYE_RECOVERY_MSEC)
		return false
	if Time.get_ticks_msec() < _stale_recovery_until_msec:
		return false
	return true

func refresh() -> void:
	if _shutdown_requested:
		return
	# A resolution/refresh-rate change defers its actual swapchain rebuild by
	# a few frames (see start()'s C++ comment) rather than tearing it down
	# synchronously; if that deferred rebuild failed, the swapchain is gone
	# and this renderer can't recover on its own -- fall back like a normal
	# start() failure.
	if active and renderer.consume_pending_resize_failure():
		failure_reason = "swapchain rebuild failed after resolution change"
		main._log("[NATIVE-XR] Deferred resize rebuild failed; falling back to legacy renderer")
		deactivate(true)
		_last_eligible = false
		return
	var eligible := _eligible()
	if not eligible:
		if active:
			deactivate(true)
		_last_eligible = false
		return
	var size := Vector2i(main.stream_backend.get_video_width(), main.stream_backend.get_video_height())
	if size.x <= 0 or size.y <= 0:
		return
	var mode := _mode()
	var full_sbs: bool = main.settings.host.effective_sbs_mode() == 3
	if not stream_started or size != _last_size or full_sbs != _last_full_sbs:
		if not renderer.start(size.x, size.y, full_sbs):
			failure_reason = "swapchain or GLES initialization failed"
			main._log("[NATIVE-XR] Start failed; retaining legacy renderer")
			deactivate(true)
			return
		stream_started = true
		_last_size = size
		_last_full_sbs = full_sbs
		main._log("[NATIVE-XR] Native stream renderer ready at %dx%d" % [size.x, size.y])
	active = true
	_last_mode = mode
	_last_eligible = true
	_sync_geometry()
	# Hand off immediately after the native swapchain is ready. Keeping the
	# legacy Texture2DRD proxies alive while a restarted decoder is replacing
	# their backing textures can make Godot's GLThread dereference a null
	# remap target. The native renderer simply remains black until its first
	# decoder frame arrives, normally less than one display interval later.
	if not legacy_disabled:
		main.stream_backend.set_native_direct_mode(true)
		if main.depth_estimator:
			main.depth_estimator.set_native_renderer_active(true)
		if main.comp:
			main.comp.clear_native_ambient_sample()
		_disable_legacy_video()
		legacy_disabled = true
		renderer.set_overlay_visible(main.settings.performance_overlay_enabled)
		if main.settings.performance_overlay_enabled:
			request_stats_overlay_update()
		main._log("[NATIVE-XR] Native renderer active; legacy full-resolution passes disabled")

func _sync_geometry() -> void:
	if not active or not main.primary_screen:
		return
	if main.screen_manager:
		main.screen_manager.ensure_pc_source_aspect()
	var screen: VRScreen = main.primary_screen
	var transform: Transform3D = screen.global_transform
	var radius: float = screen.get_cylinder_radius() if screen.curvature > 0 else 1.0
	var central_angle: float = screen.mesh_size.x / radius if screen.curvature > 0 else 0.01
	if screen.curvature > 0:
		var screen_forward: Vector3 = -screen.global_transform.basis.z
		transform.origin = screen.global_position - screen_forward * radius
	var view_dist := maxf((screen.global_position - main.xr_camera.global_position).length(), 0.5)
	var sort_order := clampi(int((10.0 - view_dist) * 10.0), 1, 100)
	var framing: bool = renderer.has_method("supports_view_framing") and renderer.supports_view_framing()
	if framing:
		var sm = main.screen_manager
		var cropped: bool = sm.crop_top + sm.crop_bottom > 0.0
		renderer.set_geometry(transform, screen.mesh_size.x, screen.mesh_size.y,
				screen.curvature, radius, central_angle, sort_order, main.settings.bezel_enabled and not cropped, sm.crop_top, sm.crop_bottom)
	else:
		renderer.set_geometry(transform, screen.mesh_size.x, screen.mesh_size.y,
				screen.curvature, radius, central_angle, sort_order, main.settings.bezel_enabled)
	renderer.set_compositor_sharpening(_native_compositor_sharpening())

func _disable_legacy_video() -> void:
	var screen = main.primary_screen
	if not screen:
		return
	for layer in [screen.comp_cylinder, screen.comp_cylinder_left, screen.comp_cylinder_right]:
		if layer:
			layer.visible = false
	for viewport in [screen.comp_viewport, screen.comp_viewport_left, screen.comp_viewport_right]:
		if viewport:
			viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
	main.stream_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED

func request_picture_refresh() -> void:
	# A paused image must respond to Picture controls without a decoder update.
	_picture_refresh_pending = true

func process_frame(new_frame: bool) -> void:
	if _shutdown_requested:
		if is_instance_valid(renderer) and renderer.is_shutdown_complete():
			renderer = null
			_shutdown_complete = true
		return
	refresh()
	_sync_video_sampling()
	_process_stats_upload()
	_process_ambient_sample()
	if not active or not (new_frame or _picture_refresh_pending):
		return
	var frame_source = main.stream_backend.get_oes_frame_source()
	if frame_source == null:
		# This is normal during decoder startup/restart. Retain the prepared
		# native swapchain and wait for the first published OES frame instead
		# of thrashing back into the legacy renderer.
		return
	var mode := _mode()
	var depth_id := 0
	var guide_id := 0
	var separation := 0.0
	if mode == 6 or mode == 10 or mode == 11:
		var depth = main.depth_estimator
		if depth and depth.depth_texture:
			guide_id = main.stream_backend.get_native_depth_guide_texture_id()
			if guide_id != 0:
				depth_id = RenderingServer.texture_get_native_handle(depth.depth_texture.get_rid())
				separation = depth._pass_parallax * (main.settings.host.ai_3d_separation_pct / 100.0)
	var convergence: float = float(main.settings.host.ai_3d_convergence_pct) / 100.0
	var brightness: float = float(main.settings.brightness_pct) / 100.0
	var contrast: float = float(main.settings.contrast_pct) / 100.0
	var gamma: float = float(main.settings.gamma_pct) / 100.0
	var color_gain := PictureColor.gain(main.settings.host.picture_temperature, main.settings.host.picture_tint)
	# Pixels, matrix, transfer and token are acquired together later on the
	# render thread, under one lease spanning both-eye draw commands.
	renderer.submit_frame(true, 0, depth_id, guide_id, PackedFloat32Array(),
			3.0, main.primary_screen.mesh_size.x, false, separation,
			false, main.settings.passthrough_enabled, 0, mode,
			main.depth_estimator.depth_revision if main.depth_estimator else 0,
			0, convergence, brightness, contrast, gamma, frame_source, color_gain)
	_picture_refresh_pending = false

func request_ambient_sample() -> void:
	if active and stream_started and is_instance_valid(renderer):
		renderer.request_ambient_sample()

func _process_ambient_sample() -> void:
	var now_ms := Time.get_ticks_msec()
	_poll_color_probe_trigger(now_ms)
	var pc_profile: Vector2i = main.settings.host.pc_profile_resolution
	var eligible: bool = active and stream_started and is_instance_valid(renderer) and main.is_streaming \
			and pc_profile != Vector2i.ZERO and renderer.has_rendered_frame()
	if not eligible:
		_log_color_probe(_color_probe.tick(now_ms, false, main.is_streaming))
	if not active or not stream_started or not is_instance_valid(renderer) or not main.comp:
		return
	var pixels: PackedByteArray = renderer.consume_ambient_sample()
	if pixels.size() == 32 * 32 * 4:
		main.comp.update_native_ambient_sample(pixels, 32, 32)
		if eligible:
			_log_color_probe(_color_probe.observe(pixels, now_ms, {
				"streaming": main.is_streaming, "native_active": active, "native_started": stream_started,
				"stream_width": _last_size.x, "stream_height": _last_size.y,
				"pc_width": pc_profile.x, "pc_height": pc_profile.y, "stereo_mode": _mode(),
				"brightness_pct": main.settings.brightness_pct,
				"contrast_pct": main.settings.contrast_pct, "gamma_pct": main.settings.gamma_pct,
				"picture_temperature": main.settings.host.picture_temperature,
				"picture_tint": main.settings.host.picture_tint}))
	var action: Dictionary = _color_probe.tick(now_ms, eligible, main.is_streaming)
	if action.get("request", false):
		renderer.request_ambient_sample()
	if action.has("event"):
		_log_color_probe(action)

func _log_color_probe(event: Dictionary) -> void:
	if not event.is_empty():
		main._log("[Quest3DColorProbe] " + JSON.stringify(event))

func _poll_color_probe_trigger(now_ms: int) -> void:
	if not _color_probe.enabled or now_ms - _color_probe_poll_ms < 1000:
		return
	_color_probe_poll_ms = now_ms
	if FileAccess.file_exists(COLOR_PROBE_REQUEST):
		var file := FileAccess.open(COLOR_PROBE_REQUEST, FileAccess.READ)
		if file:
			var length := file.get_length()
			# Never parse an unbounded file and never execute any request content.
			var data := file.get_buffer(length) if length > 0 and length <= 512 else PackedByteArray()
			file.close()
			var fingerprint := data.hex_encode().sha256_text()
			if fingerprint != _color_probe_request_hash:
				_color_probe_request_hash = fingerprint
				var request: Variant = JSON.parse_string(data.get_string_from_utf8()) if not data.is_empty() else null
				_log_color_probe(_color_probe.rearm(request, int(Time.get_unix_time_from_system() * 1000.0),
					now_ms, OS.get_process_id()))
			DirAccess.remove_absolute(COLOR_PROBE_REQUEST)
	# Debug-only observation, not a command or product control channel. Atomic
	# replacement prevents a host read from mistaking a partial JSON for state.
	var status_file := FileAccess.open(COLOR_PROBE_STATUS + ".tmp", FileAccess.WRITE)
	if status_file:
		status_file.store_string(JSON.stringify(_color_probe.status(OS.get_process_id())))
		status_file.close()
		DirAccess.rename_absolute(COLOR_PROBE_STATUS + ".tmp", COLOR_PROBE_STATUS)

func request_stats_overlay_update() -> void:
	if active:
		if is_instance_valid(renderer) and stream_started:
			renderer.set_overlay_visible(main.settings.performance_overlay_enabled)
		_stats_upload_delay = 1

func set_stats_visible(value: bool) -> void:
	if is_instance_valid(renderer) and stream_started:
		renderer.set_overlay_visible(value)
	if value:
		request_stats_overlay_update()

func _process_stats_upload() -> void:
	if _stats_upload_delay < 0 or not active or not renderer.has_rendered_frame():
		return
	if _stats_upload_delay > 0:
		_stats_upload_delay -= 1
		return
	_stats_upload_delay = -1
	if not main.comp or not main.comp.stats_viewport:
		return
	var image: Image = main.comp.stats_viewport.get_texture().get_image()
	if image == null or image.is_empty():
		return
	if image.get_format() != Image.FORMAT_RGBA8:
		image.convert(Image.FORMAT_RGBA8)
	if image.get_size() != Vector2i(768, 512):
		image.resize(768, 512)
	# Godot Images are top-left-origin while glTexSubImage2D feeds the OpenXR
	# overlay texture bottom-left-origin data.
	image.flip_y()
	renderer.upload_overlay(image.get_data(), 768, 512)

func deactivate(restore_legacy: bool) -> void:
	if _shutdown_requested:
		return
	if main.stream_backend:
		main.stream_backend.set_native_direct_mode(false)
	if main.depth_estimator:
		main.depth_estimator.set_native_renderer_active(false)
	active = false
	legacy_disabled = false
	_stats_upload_delay = -1
	if main.comp:
		main.comp.clear_native_ambient_sample()
	if stream_started and is_instance_valid(renderer):
		renderer.stop_stream()
	stream_started = false
	_last_size = Vector2i.ZERO
	if restore_legacy and main.is_streaming and main.comp and main.comp.available:
		var mode := _mode()
		if mode > 0:
			main.comp.switch_to_stereo_comp_layer()
		else:
			main.comp.switch_to_comp_layer()
		# Re-sync the legacy overlay now that this renderer is no longer the
		# one presenting it - see toggle_performance_overlay()'s comment for
		# why the two display paths must stay mutually exclusive.
		main.comp.set_stats_visible(main.settings.performance_overlay_enabled and main.is_streaming)

func shutdown(scene_attached: bool = true) -> void:
	if _shutdown_complete:
		return
	var first_request := not _shutdown_requested
	_shutdown_requested = true
	scene_attached = scene_attached and is_instance_valid(main) and main.is_inside_tree()
	if scene_attached and main.stream_backend:
		main.stream_backend.set_native_direct_mode(false)
	if scene_attached and main.depth_estimator:
		main.depth_estimator.set_native_renderer_active(false)
	active = false
	legacy_disabled = false
	stream_started = false
	if scene_attached and main.comp:
		main.comp.clear_native_ambient_sample()
	if is_instance_valid(renderer):
		_shutdown_complete = renderer.request_dispose()
		if _shutdown_complete:
			renderer = null
		else:
			# Native renderer is an Object with explicit disposal. Dropping this
			# manager cannot free it before its real owner-boundary completion.
			failure_reason = "native shutdown remains unconfirmed"
	elif first_request and renderer == null:
		_shutdown_complete = true
	else:
		failure_reason = "native shutdown remains unconfirmed"
	provider_registered = false

func _on_native_shutdown_completed() -> void:
	# May arrive after scene detach; do not access main here.
	_shutdown_complete = true
	renderer = null

func get_warp_gpu_ms() -> float:
	return renderer.get_warp_gpu_ms() if active and is_instance_valid(renderer) else 0.0

func query_pointer_candidate(ray: RayCast3D, eye: int = 0, retain: bool = false) -> Dictionary:
	if not active or not stream_started or not is_instance_valid(renderer) or not main.is_xr_active or not main.is_streaming \
			or main.settings.host.pc_profile_resolution == Vector2i.ZERO:
		return PcPointerProbe.rejected("pc_native_unavailable")
	if not ray or not ray.enabled or not ray.is_inside_tree():
		return PcPointerProbe.rejected("no_tracked_ray")
	# Observe pending screen edits before comparing the submitted generation.
	_sync_geometry()
	var direction: Vector3 = ray.global_basis * ray.target_position
	if retain:
		return renderer.issue_pointer_ticket(ray.global_position, direction, direction.length(), eye)
	return renderer.query_pointer_candidate(ray.global_position, direction, direction.length(), eye)

func take_pointer_ticket(ticket: int) -> Dictionary:
	if not active or not stream_started or not is_instance_valid(renderer) or not main.is_xr_active or not main.is_streaming \
			or main.settings.host.pc_profile_resolution == Vector2i.ZERO:
		return PcPointerProbe.rejected("pc_native_unavailable")
	_sync_geometry()
	return renderer.take_pointer_ticket(ticket)
