class_name ScreenManager
extends RefCounted

var main: Node3D

var view_locked := false
var keep_view_size := false
var saved_view: Dictionary = {}
var view_name := "Custom"
var view_message := ""
var menu_scale := 1.0
var pointer_scale := 1.0
var pointer_contrast := false
var crop_top := 0.0
var crop_bottom := 0.0

func _init(owner: Node3D):
	main = owner

func content_size_for_screen(s: VRScreen, stream_size: Vector2i) -> Vector2i:
	if stream_size.x <= 0 or stream_size.y <= 0:
		return Vector2i.ZERO
	var size := Vector2i(int(stream_size.x * s.uv_region.z), int(stream_size.y * s.uv_region.w))
	if main.settings.host.effective_sbs_mode() == 3 and s == main.primary_screen:
		if size.x % 2 != 0:
			return Vector2i.ZERO
		size.x = size.x / 2
	return size if size.x > 0 and size.y > 0 else Vector2i.ZERO

func presentation_content_size(s: VRScreen) -> Vector2i:
	var stream_size: Vector2i = main.settings.host.pc_profile_resolution
	if stream_size == Vector2i.ZERO and main.is_streaming and main.stream_backend:
		stream_size = Vector2i(main.stream_backend.get_video_width(), main.stream_backend.get_video_height())
	if stream_size.x > 0 and stream_size.y > 0:
		return content_size_for_screen(s, stream_size)
	if s.monitor:
		var size: Vector2i = s.monitor.frame_rect.size
		if main.settings.host.effective_sbs_mode() == 3 and s == main.primary_screen:
			return content_size_for_screen(s, main.compute_requested_resolution())
		return size
	return Vector2i.ZERO

func presentation_aspect(s: VRScreen) -> float:
	var content := presentation_content_size(s)
	if content.x > 0 and content.y > 0:
		return float(content.x) / (content.y * framing_height_fraction(s))
	return s.mesh_size.x / s.mesh_size.y if s.mesh_size.y > 0.0 else 0.0

func ensure_pc_source_aspect() -> bool:
	# A saved placement or a placeholder monitor layout may arrive after the
	# pinned PC profile. Keep its chosen width/pose, but never stretch the
	# per-eye content to that placement's old aspect.
	var s: VRScreen = main.primary_screen
	if not s or main.settings.host.pc_profile_resolution == Vector2i.ZERO:
		return false
	var content := content_size_for_screen(s, main.settings.host.pc_profile_resolution)
	if content == Vector2i.ZERO or not is_finite(s.mesh_size.x) or s.mesh_size.x <= 0.0:
		return false
	var expected_height := s.mesh_size.x * float(content.y) / content.x * framing_height_fraction(s)
	if is_finite(s.mesh_size.y) and absf(s.mesh_size.y - expected_height) < 0.0001:
		return false
	_invalidate_presentation_input()
	s.mesh_size.y = expected_height
	s.apply_curvature()
	s.update_corner_positions()
	s.update_bezel_size()
	if main.comp != null and main.comp.available:
		main.comp.update_cylinder_params()
	return true

func _invalidate_presentation_input():
	if main.pc_control:
		main.pc_control.pointer.invalidate("Screen size or distance changed")

func _finish_presentation_change():
	# Geometry-only: no decoder dimensions, render targets, or stream restart.
	# Native refresh/query/take_pointer_ticket observes this transform before
	# accepting a ticket, and set_geometry invalidates the submitted generation.
	if main.comp != null and main.comp.available:
		main.comp.update_layer_size()
	if main.state_manager:
		main.state_manager.save_host_state()

func view_distance() -> float:
	if not main.primary_screen or not main.xr_camera:
		return 0.0
	return main.primary_screen.global_position.distance_to(main.xr_camera.global_position)

func view_width_limits(distance: float = -1.0, curve: int = -1) -> Vector2:
	var s: VRScreen = main.primary_screen
	if not s:
		return Vector2.ZERO
	var aspect := presentation_aspect(s)
	if not is_finite(aspect) or aspect <= 0.0:
		return Vector2.ZERO
	var mode := s.curvature if curve < 0 else curve
	var maximum := 12.0
	if mode > 0:
		var radius := 10.0 if mode == 1 else 4.0
		if main.comp and main.comp.in_use:
			radius = maxf(0.5, view_distance() if distance < 0 else distance) * (3.0 if mode == 1 else 2.0)
		maximum = minf(maximum, PI * radius)
	return Vector2(maxf(0.6, 0.4 * aspect), maximum)

func _view_changed(label: String = "Custom"):
	var s: VRScreen = main.primary_screen
	_invalidate_presentation_input()
	s.apply_curvature()
	s.update_corner_positions()
	s.update_bezel_size()
	view_name = label
	_finish_presentation_change()

func set_view_width(width: float) -> bool:
	if view_locked or not main.primary_screen or not is_finite(width):
		return false
	var bounds := view_width_limits()
	if bounds.x <= 0 or bounds.x > bounds.y:
		return false
	var next := clampf(width, bounds.x, bounds.y)
	view_message = "크기 한계" if not is_equal_approx(next, width) else ""
	main.primary_screen.mesh_size = Vector2(next, next / presentation_aspect(main.primary_screen))
	_view_changed()
	return true

func scale_primary_screen(factor: float) -> bool:
	var s: VRScreen = main.primary_screen
	if not s or not is_finite(factor) or factor <= 0.0 or not is_finite(s.mesh_size.x) or s.mesh_size.x <= 0.0:
		return false
	return set_view_width(s.mesh_size.x * factor)

func move_primary_screen(delta_metres: float) -> bool:
	if not is_finite(delta_metres):
		return false
	return set_view_distance(view_distance() + delta_metres)

func set_view_distance(metres: float) -> bool:
	var s: VRScreen = main.primary_screen
	var camera = main.xr_camera
	if view_locked or not s or not camera or not is_finite(metres):
		return false
	var offset: Vector3 = s.global_position - camera.global_position
	var distance := offset.length()
	if not is_finite(distance) or distance < 0.001 or not s.mesh_size.is_finite() or s.mesh_size.x <= 0 or presentation_aspect(s) <= 0:
		return false
	var minimum_distance := 0.5
	var maximum_distance := 10.0
	if keep_view_size:
		if s.curvature > 0 and (not main.comp or not main.comp.in_use):
			view_message = "곡면 크기 고정 · PC 영상 연결 후 조절"
			return false
		# Scale the entire camera-relative geometry uniformly, including the
		# cylinder radius (which is proportional to distance). Both eye poses
		# remain independent; this preserves the centre-view apparent size.
		var bounds := view_width_limits()
		minimum_distance = maxf(minimum_distance, distance * bounds.x / s.mesh_size.x)
		maximum_distance = minf(maximum_distance, distance * 12.0 / s.mesh_size.x)
	elif s.curvature > 0 and main.comp != null and main.comp.in_use:
		# Keep the cylinder under a half turn as distance changes its radius.
		var radius_factor := 3.0 if s.curvature == 1 else 2.0
		minimum_distance = maxf(minimum_distance, s.mesh_size.x / (PI * radius_factor))
	if minimum_distance > maximum_distance:
		return false
	var next := clampf(metres, minimum_distance, maximum_distance)
	var width := s.mesh_size.x * next / distance if keep_view_size else s.mesh_size.x
	var limits := view_width_limits(next)
	if width < limits.x - 0.0001 or width > limits.y + 0.0001:
		view_message = "크기·거리 한계"
		return false
	view_message = "거리 한계" if not is_equal_approx(next, metres) else ""
	s.global_position = camera.global_position + offset / distance * next
	s.mesh_size = Vector2(width, width / presentation_aspect(s))
	_view_changed()
	return true

func shift_view(step: Vector2) -> bool:
	if view_locked or not step.is_finite() or not main.primary_screen or not main.xr_camera:
		return false
	var s: VRScreen = main.primary_screen
	var basis: Basis = main.xr_camera.global_basis.orthonormalized()
	var position: Vector3 = s.global_position + basis.x * step.x + basis.y * step.y
	var distance: float = position.distance_to(main.xr_camera.global_position)
	if distance < 0.5 or distance > 10.0 or s.mesh_size.x > view_width_limits(distance).y:
		view_message = "위치 한계"
		return false
	s.global_position = position
	view_message = ""
	_view_changed()
	return true

func tilt_view(degrees: float) -> bool:
	if view_locked or not is_finite(degrees) or not main.primary_screen:
		return false
	main.primary_screen.rotate_object_local(Vector3.RIGHT, deg_to_rad(clampf(degrees, -10, 10)))
	view_message = ""
	_view_changed()
	return true

func center_view() -> bool:
	if view_locked or not main.primary_screen or not main.xr_camera:
		return false
	var camera: Transform3D = main.xr_camera.global_transform.orthonormalized()
	main.primary_screen.global_transform = Transform3D(camera.basis, camera.origin - camera.basis.z * clampf(view_distance(), 0.5, 10.0))
	view_message = "정면 배치"
	_view_changed()
	return true

func level_view() -> bool:
	if view_locked or not main.primary_screen or not main.xr_camera:
		return false
	var s: VRScreen = main.primary_screen
	var forward := -s.global_basis.z.normalized()
	forward.y = 0.0
	if forward.length_squared() < 0.0001:
		forward = -main.xr_camera.global_basis.z
		forward.y = 0.0
	if forward.length_squared() < 0.0001:
		forward = Vector3.FORWARD
	s.global_basis = Basis.looking_at(forward.normalized(), Vector3.UP)
	view_message = "수평 맞춤"
	_view_changed()
	return true

func set_view_curve(curve: int) -> bool:
	if view_locked or curve < 0 or curve > 2 or not main.primary_screen:
		return false
	var bounds := view_width_limits(-1, curve)
	if main.primary_screen.mesh_size.x > bounds.y:
		view_message = "곡률 변경 전 크기 축소"
		return false
	main.curvature = curve
	main.primary_screen.curvature = curve
	view_message = ""
	_view_changed()
	return true

func set_view_locked(value: bool):
	view_locked = value
	if value:
		if main.grabbed_node is VRScreen:
			main.grabbed_node = null
			main.grabbed_bar = null
			main.grab_group_start_transforms.clear()
		if main.grabbed_corner_screen:
			main.grabbed_corner_screen.end_corner_resize()
			main.grabbed_corner_screen = null
			main.grabbed_corner_idx = -1
		if main.xr_interaction:
			main.xr_interaction._corner_resize_started = false
	view_message = "위치 잠금" if value else "잠금 해제"
	_save_view_options()

func set_keep_view_size(value: bool):
	keep_view_size = value
	_save_view_options()

func _save_view_options():
	if main.state_manager:
		main.state_manager.save_host_state()

func capture_view() -> Dictionary:
	if not main.primary_screen or not main.xr_camera:
		return {}
	var relative: Transform3D = main.xr_camera.global_transform.orthonormalized().affine_inverse() * main.primary_screen.global_transform.orthonormalized()
	return {"version": 1, "position": [relative.origin.x, relative.origin.y, relative.origin.z],
		"rotation": [relative.basis.get_euler().x, relative.basis.get_euler().y, relative.basis.get_euler().z],
		"width": main.primary_screen.mesh_size.x, "curvature": main.primary_screen.curvature, "locked": view_locked}

func _finite_triplet(value: Variant) -> bool:
	if not value is Array or value.size() != 3:
		return false
	for number in value:
		if not (number is int or number is float) or not is_finite(float(number)):
			return false
	return true

func valid_saved_view(value: Variant) -> bool:
	if not value is Dictionary or value.get("version") != 1:
		return false
	if not _finite_triplet(value.get("position")) or not _finite_triplet(value.get("rotation")):
		return false
	var width: Variant = value.get("width")
	var curve: Variant = value.get("curvature")
	var p: Array = value.position
	var distance := Vector3(p[0], p[1], p[2]).length()
	return (width is int or width is float) and is_finite(float(width)) and width >= 0.6 and width <= 12 \
		and curve is int and curve >= 0 and curve <= 2 and distance >= 0.5 and distance <= 10.0

func save_view() -> bool:
	var candidate := capture_view()
	if not valid_saved_view(candidate):
		return false
	saved_view = candidate
	view_message = "내 보기 저장됨"
	_save_view_options()
	return true

func apply_view_preset(preset: String) -> bool:
	if view_locked or not main.primary_screen or not main.xr_camera:
		return false
	var data: Dictionary
	match preset:
		"Standard": data = {"version": 1, "position": [0.0, 0.0, -2.5], "rotation": [0.0, 0.0, 0.0], "width": 2.4, "curvature": 0}
		"Cinema": data = {"version": 1, "position": [0.0, 0.0, -3.0], "rotation": [0.0, 0.0, 0.0], "width": 4.0, "curvature": 1}
		"Reclined":
			if not center_view():
				return false
			view_name = preset
			view_message = "현재 시선에 배치"
			return true
		"My view": data = saved_view.duplicate(true)
		_: return false
	if not valid_saved_view(data):
		view_message = "저장된 보기 없음"
		return false
	var position: Array = data.position
	var rotation: Array = data.rotation
	var local := Transform3D(Basis.from_euler(Vector3(rotation[0], rotation[1], rotation[2])), Vector3(position[0], position[1], position[2]))
	var bounds := view_width_limits(local.origin.length(), int(data.curvature))
	if float(data.width) < bounds.x or float(data.width) > bounds.y:
		view_message = "현재 화면비에 맞지 않는 보기"
		return false
	main.primary_screen.global_transform = main.xr_camera.global_transform.orthonormalized() * local
	main.primary_screen.mesh_size = Vector2(data.width, data.width / presentation_aspect(main.primary_screen))
	main.curvature = int(data.curvature)
	main.primary_screen.curvature = main.curvature
	view_message = ""
	_view_changed(preset)
	if data.get("locked") is bool and data.locked:
		set_view_locked(true)
	return true

func view_options() -> Dictionary:
	return {"version": 1, "locked": view_locked, "keep_size": keep_view_size, "saved": saved_view.duplicate(true),
		"name": view_name,
		"menu_scale": menu_scale, "pointer_scale": pointer_scale, "pointer_contrast": pointer_contrast,
		"crop_top": crop_top, "crop_bottom": crop_bottom}

func restore_view_options(value: Variant):
	view_locked = false
	keep_view_size = false
	saved_view = {}
	menu_scale = 1.0
	pointer_scale = 1.0
	pointer_contrast = false
	crop_top = 0.0
	crop_bottom = 0.0
	view_name = "Custom"
	view_message = ""
	if value is Dictionary and value.get("version") == 1:
		if value.get("name") in ["Custom", "Standard", "Cinema", "Reclined", "My view"]:
			view_name = value.name
		view_locked = value.get("locked") is bool and value.locked
		keep_view_size = value.get("keep_size") is bool and value.keep_size
		if valid_saved_view(value.get("saved")):
			saved_view = value.saved.duplicate(true)
		for key in ["menu_scale", "pointer_scale"]:
			var number: Variant = value.get(key, 1.0)
			if (number is int or number is float) and is_finite(float(number)):
				set(key, clampf(float(number), 0.8, 1.5))
		pointer_contrast = value.get("pointer_contrast") is bool and value.pointer_contrast
		for key in ["crop_top", "crop_bottom"]:
			var number: Variant = value.get(key, 0.0)
			if (number is int or number is float) and is_finite(float(number)):
				set(key, clampf(float(number), 0.0, 0.25))
	apply_menu_scale()
	apply_pointer_style()

func set_menu_scale(value: float):
	if not is_finite(value):
		return
	menu_scale = clampf(value, 0.8, 1.5)
	apply_menu_scale()
	_save_view_options()

func apply_menu_scale():
	if not main.ui_panel_3d:
		return
	# Scale the real panel/collider together. Ray mapping uses to_local(), so
	# logical 1200x580 coordinates and the bottom move handle stay unchanged.
	main.ui_panel_3d.scale = Vector3.ONE * menu_scale
	if main.comp_ui:
		main.comp_ui.set_quad_size(main._ui_mesh_size * menu_scale)

func center_menu():
	if not main.ui_panel_3d or not main.xr_camera:
		return
	var camera: Transform3D = main.xr_camera.global_transform.orthonormalized()
	main.ui_panel_3d.global_transform = Transform3D(camera.basis.scaled(Vector3.ONE * menu_scale), camera.origin - camera.basis.z * 1.5)
	main._save_ui_offset()

func set_pointer_style(scale_value: float, contrast: bool):
	if not is_finite(scale_value):
		return
	pointer_scale = clampf(scale_value, 0.8, 1.5)
	pointer_contrast = contrast
	apply_pointer_style()
	_save_view_options()

func apply_pointer_style():
	for viewport in [main.comp_cursor_viewport, main.left_comp_cursor_viewport]:
		if viewport:
			var circle = viewport.get_node_or_null("CircleTexture")
			if circle and circle.material is ShaderMaterial:
				circle.material.set_shader_parameter("high_contrast", pointer_contrast)

func framing_available() -> bool:
	if not main.video_presentation or not main.video_presentation.is_native_active():
		return false
	var renderer = main.video_presentation.native_renderer.renderer
	return renderer != null and renderer.has_method("supports_view_framing") and renderer.supports_view_framing()

func framing_height_fraction(s: VRScreen) -> float:
	if s != main.primary_screen or not framing_available():
		return 1.0
	var content := presentation_content_size(s)
	if content.y <= 0: return 1.0
	# The native swapchain has a fixed 8-pixel margin on each edge, even
	# with Bezel off. Match its rounded sub-image rows exactly.
	var height := content.y + 16
	return float(height - roundi(height * crop_top) - roundi(height * crop_bottom)) / height

func set_view_crop(top: float, bottom: float) -> bool:
	if view_locked or not is_finite(top) or not is_finite(bottom): return false
	if (top > 0 or bottom > 0) and not framing_available(): return false
	crop_top = clampf(top, 0.0, 0.25)
	crop_bottom = clampf(bottom, 0.0, 0.25)
	if main.primary_screen:
		main.primary_screen.mesh_size.y = main.primary_screen.mesh_size.x / presentation_aspect(main.primary_screen)
		_view_changed()
	else:
		_save_view_options()
	return true

func create_corner_handles():
	main.primary_screen.create_corner_handles()

func create_corner_handles_for(s: VRScreen):
	s.create_corner_handles()

func update_corner_positions():
	main.primary_screen.update_corner_positions()

func create_bezel():
	main.primary_screen.create_bezel()

func create_bezel_for(s: VRScreen):
	s.create_bezel()

func update_bezel_size():
	for s in main.screens:
		s.update_bezel_size()

func toggle_bezel():
	set_bezel_enabled(not main.settings.bezel_enabled)

func set_bezel_enabled(enabled: bool):
	main.settings.bezel_enabled = enabled
	for s in main.screens:
		if s.bezel_mesh:
			s.bezel_mesh.visible = main.settings.bezel_enabled if not main.comp.in_use else false
	main.ui_controller.update_option_btn(main._ui_bezel_btn, "On" if main.settings.bezel_enabled else "Off")
	main.comp.update_bezel()
	main.state_manager.save_state()

# GitHub issue #17 (2026-08-20): used to prefer s.monitor.frame_rect.size
# over the real stream_w/stream_h whenever a monitor spec existed - correct
# for genuine multi-monitor manifests (Polaris hosts report each monitor's
# real per-output resolution there), but WRONG for any single-screen/non-
# manifest host (Sunshine et al.): s.monitor there is just whatever the
# welcome screen's fixed 16:9 placeholder last set, never updated again, so
# the mesh stayed 16:9 forever regardless of the actual stream's real aspect
# (a 21:9 source got squeezed into a 16:9-shaped quad). A shader-side
# letterbox/pillarbox fix was tried first and worked, but the better fix is
# this: always resize the mesh itself to the REAL decoded content's aspect -
# resize_to_aspect() below already keeps WIDTH anchored to mesh_size.x (i.e.
# whatever this screen's current grid-cell footprint width already is) and
# only adjusts height, so a 21:9 screen ends up shorter (fits the same grid
# column, less vertical space) and a 4:3 screen taller - the actual visible
# mesh shape now matches reality instead of hiding the mismatch behind black
# bars. s.uv_region encodes what fraction of the (possibly multi-monitor
# composite) stream_w/stream_h this particular screen shows - use just its
# own slice, not the whole composite, or a genuine future multi-monitor
# layout would misjudge every screen's fit against the WRONG total. Height
# growing past a single grid row (e.g. a 4:3 source) can currently overlap a
# vertical neighbor - a real but deliberately deferred gap, not something
# this fix attempts to solve yet (see MULTI_MONITOR grid work).
func resize_screen_to_aspect(stream_w: int, stream_h: int):
	# During a PC session a generic saved layout can still be 1920x1080.
	# It is neither the packed 3200x900 canvas nor a per-eye size. The pinned
	# profile stays authoritative for presentation even when a layout reflows.
	var source_size: Vector2i = main.settings.host.pc_profile_resolution
	if source_size == Vector2i.ZERO:
		source_size = Vector2i(stream_w, stream_h)
	for s in main.screens:
		var content := content_size_for_screen(s, source_size)
		if content != Vector2i.ZERO:
			s.resize_to_aspect(content.x, content.y)
	if main.comp_layer and main.comp_layer is OpenXRCompositionLayerQuad:
		main.comp_layer.set_quad_size(main._mesh_size)
	# GitHub issue #17 fix follow-up (found 2026-08-20): resize_to_aspect()
	# above changes mesh_size (real content aspect, no longer a fixed 16:9
	# "frame") but does NOT touch the composition-layer cylinder's own
	# radius/central_angle (_comp_cyl_radius/_comp_cyl_central_angle,
	# computed by update_cylinder_params() from mesh_size) - leaving them
	# stale relative to the new mesh height. Those stale params drive BOTH
	# the actual OpenXR cylinder layer's visual shape AND
	# vr_screen.gd's hit_point_to_uv() click hit-testing (the comp.in_use
	# branch re-projects the raycast onto a cylinder of THIS radius) -
	# confirmed live: vertical click position was correct at screen center
	# but increasingly wrong toward the top/bottom edges, exactly the
	# "cylinder radius doesn't match the real geometry" signature. Every
	# OTHER caller that resizes mesh_size already pairs it with an explicit
	# update_cylinder_params() call (see settings_controller.gd's
	# apply_screen_layout()) - stream_manager.gd's resize_stream_viewport()
	# (the path actually active for local-capture-mode streaming) was the
	# one missing it. Doing it here instead of at each call site means no
	# future caller can miss this pairing again.
	if main.comp != null and main.comp.available:
		main.comp.update_cylinder_params()

func cycle_curvature():
	if view_locked:
		return
	main.curvature = (main.curvature + 1) % 3
	for s in main.screens:
		s.curvature = main.curvature
		s.apply_curvature()
	main.settings_controller.reflow_grid_screens()
	# comp.in_use reflects whichever composition layer was last explicitly
	# switched to, not whether legacy is the renderer actually presenting
	# right now - it stays stale (true) from an earlier fallback (e.g. a
	# has_stale_eye_layer() cooldown) even after native_xr_renderer.gd's own
	# refresh() has since silently reclaimed the display and disabled the
	# legacy viewports again. Calling switch_to_comp_layer() here while
	# native is the one actually active reactivates the legacy composition
	# layer's own OpenXR swapchain with nothing to ever disable it again
	# (legacy_disabled only gets cleared by deactivate(), which this doesn't
	# call) - a second, permanently-live composition layer fighting the
	# native one over the same screen, seen on-device as one eye going black/
	# frozen right after a curvature change. Native's own _sync_geometry()
	# already re-syncs curvature into its layer every frame regardless, so
	# this call is only ever needed when legacy is genuinely presenting.
	var native_active = main.video_presentation != null and main.video_presentation.is_native_active()
	if main.comp.in_use and not native_active:
		main.comp.switch_to_comp_layer()
	main.ui_controller.update_option_btn(main._ui_curve_btn, main.curvature_labels[main.curvature])
	main.state_manager.save_state()

func apply_curvature():
	for s in main.screens:
		s.apply_curvature()

func _get_cylinder_radius() -> float:
	return main.primary_screen.get_cylinder_radius()
