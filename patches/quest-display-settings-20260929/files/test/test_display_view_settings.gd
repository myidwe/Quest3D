extends SceneTree

class QuietMain extends "res://main.gd":
	func _ready(): pass
	func _process(_delta): pass
	func _notification(_what): pass

class NoRestart extends SettingsController:
	var restarts := 0
	func _schedule_stream_restart(): restarts += 1

class RecordedRenderer extends RefCounted:
	var calls: Array = []
	func supports_view_framing() -> bool: return true
	func set_geometry(pose, width, height, curve, radius, angle, order, bezel, top = 0.0, bottom = 0.0):
		calls.append({"pose": pose, "width": width, "height": height, "curve": curve, "radius": radius, "angle": angle, "order": order, "bezel": bezel, "top": top, "bottom": bottom})
	func set_compositor_sharpening(_value): pass

var failures: Array[String] = []
var checked := 0
func check(value: bool, message: String):
	checked += 1
	if not value:
		failures.append(message)
		printerr("REGRESSION FAIL ", message)

func _init(): _run.call_deferred()

func click(ui, name: String):
	var button: Button = ui.pages[0].find_child(name, true, false)
	check(button != null and button.is_visible_in_tree(), name + " reachable")
	if button and not button.disabled:
		button.button_down.emit()
		button.button_up.emit()

func _run():
	check(OS.get_environment("NIGHTFALL_FIRST_RUN_FIXTURE") == "1", "Isolated test profile required")
	if not failures.is_empty():
		quit(1)
		return
	if FileAccess.file_exists("user://host_state.cfg"):
		check(DirAccess.remove_absolute("user://host_state.cfg") == OK, "Reset only isolated fixture config")
	var app = load("res://main.tscn").instantiate()
	app.set_script(QuietMain)
	root.add_child(app)
	app.screen_registry.initialize(app.screen_mesh)
	app.layout = ScreenLayout.single(Vector2i(4096, 1152))
	app.primary_screen.setup(app)
	app.primary_screen.create_corner_handles()
	app.primary_screen.apply_monitor(app.layout.monitors[0], app.layout.frame_size)
	app.screen_manager = ScreenManager.new(app)
	app.settings_controller = NoRestart.new(app)
	app.controller_mapper = ControllerMapper.new(app)
	app.add_child(app.controller_mapper)
	app.state_manager = StateManager.new(app)
	app.pc_control = PcControl.new(app)
	app.xr_interaction = XRInteraction.new(app)
	app.xr_camera = XRCamera3D.new()
	app.add_child(app.xr_camera)
	app.ui_controller = UIController.new(app)
	app.settings.host.pc_profile_resolution = Vector2i(4096, 1152)
	app.primary_screen.mesh_size = Vector2(2.4, 1.35)
	app.primary_screen.position = Vector3(0, 0, -3)
	app.primary_screen.curvature = 0
	app.ui_visible = true
	app.ui_panel_3d.show()
	app.get_node("%IPInput").text = "view-fixture"
	app.ui_controller.build_ui()
	await process_frame
	await process_frame
	var sm: ScreenManager = app.screen_manager
	var s: VRScreen = app.primary_screen
	var ui: PrecisionUI = app.ui_controller.precision
	var profile: Vector2i = app.settings.host.pc_profile_resolution
	var host_before := ConfigFile.new()
	SettingsPersistence.write_host(host_before, "host", app.settings.host)
	var stable_host := host_before.encode_to_text()
	var pose := s.global_transform
	click(ui, "SizePlus")
	check(is_equal_approx(s.mesh_size.x, 2.52) and s.global_transform == pose, "Size changes width by 5%, keeps pose")
	click(ui, "SizeMinus")
	check(s.mesh_size.is_equal_approx(Vector2(2.4, 1.35)), "Size round trip preserves per-eye aspect")
	check(ui._repeat_button == null and not ui._repeat_action.is_valid(), "Release clears repeat callback ownership")
	var repeat_button: Button = ui.view_panels.Overview.get_node("SizePlus")
	repeat_button.button_down.emit()
	app.ui_visible = false
	ui._repeat_view_action()
	check(ui._repeat_button == null and not ui._repeat_action.is_valid(), "Closing menu cancels held repeat callback")
	app.ui_visible = true
	repeat_button.button_down.emit()
	var holding_ui: WeakRef = weakref(ui)
	repeat_button.button_up.emit()
	click(ui, "SizeMinus")
	click(ui, "SizeMinus")
	click(ui, "PositionOpen")
	click(ui, "DistancePlus")
	check(is_equal_approx(sm.view_distance(), 3.1) and is_equal_approx(s.mesh_size.x, 2.4), "Distance changes metres only")
	click(ui, "KeepSize")
	var ratio := s.mesh_size.x / sm.view_distance()
	click(ui, "DistancePlus")
	check(is_equal_approx(s.mesh_size.x / sm.view_distance(), ratio), "Keep size maintains centre-view angle")
	click(ui, "Move2")
	check(is_equal_approx(s.position.y, 0.1), "Move uses camera up")
	var before_tilt := s.global_transform
	click(ui, "TiltPlus")
	check(s.position == before_tilt.origin and not s.basis.is_equal_approx(before_tilt.basis), "Tilt changes only rotation")
	s.rotate_object_local(Vector3.FORWARD, 0.3)
	click(ui, "Level")
	check(absf(s.basis.x.y) < 0.0001, "Level removes roll without moving screen")
	click(ui, "PositionBack")
	click(ui, "Lock")
	var locked_pose := s.global_transform
	var locked_size := s.mesh_size
	check(not sm.set_view_width(5) and not sm.move_primary_screen(1) and not sm.shift_view(Vector2.ONE) and not sm.tilt_view(2) and not sm.center_view() and not sm.set_view_curve(2), "Every screen geometry entry respects Lock")
	app.grabbed_node = s
	app.xr_interaction.handle_grab()
	check(app.grabbed_node == null and s.global_transform == locked_pose, "Lock cancels an in-flight screen grab")
	check(s.mesh_size == locked_size, "Locked size unchanged")
	click(ui, "Lock")
	app.xr_camera.position = Vector3(0.4, 0.8, 0.2)
	app.xr_camera.rotation = Vector3(0.3, 0.4, 0)
	var width_before := s.mesh_size.x
	var distance_before := sm.view_distance()
	click(ui, "Center")
	check(is_equal_approx(sm.view_distance(), distance_before) and s.mesh_size.x == width_before, "Center preserves size and distance")
	check(app.ui_visible, "Center leaves settings open")
	click(ui, "ViewsOpen")
	click(ui, "SaveView")
	var saved := sm.saved_view.duplicate(true)
	click(ui, "Preset1")
	check(s.curvature == 1 and is_equal_approx(s.mesh_size.x, 4) and is_equal_approx(sm.view_distance(), 3), "Cinema changes geometry only")
	app.xr_camera.position += Vector3(1, 0.3, 0.2)
	click(ui, "Preset3")
	check(is_equal_approx(s.mesh_size.x, saved.width), "My view restores width")
	var relative: Transform3D = app.xr_camera.global_transform.orthonormalized().affine_inverse() * s.global_transform
	check(relative.origin.is_equal_approx(Vector3(saved.position[0], saved.position[1], saved.position[2])), "My view restores relative to current tracking space")
	click(ui, "Preset2")
	check(app.ui_visible and s.basis.is_equal_approx(app.xr_camera.basis), "Reclined aligns once to gaze without closing menu")
	var bad: Dictionary = saved.duplicate(true)
	bad.position = [NAN, 0, -3]
	check(not sm.valid_saved_view(bad), "Malformed preset rejected")
	sm.restore_view_options({"version": 1, "menu_scale": NAN, "pointer_scale": INF, "saved": bad, "crop_top": "bad"})
	check(sm.menu_scale == 1 and sm.pointer_scale == 1 and sm.saved_view.is_empty() and sm.crop_top == 0, "Invalid settings stay finite")
	sm.restore_view_options({"version": 2, "menu_scale": 1.5})
	check(sm.menu_scale == 1, "Unknown preference version not interpreted")
	sm.set_menu_scale(1.2)
	var logical := Vector3(0.2, 0.1, 0)
	check(app.ui_panel_3d.to_local(app.ui_panel_3d.to_global(logical)).is_equal_approx(logical), "Menu scale preserves ray coordinate inverse")
	check(app.ui_panel_3d.scale.is_equal_approx(Vector3.ONE * 1.2), "Menu mesh and collider scaled together")
	sm.center_menu()
	check(is_equal_approx(app.ui_panel_3d.global_position.distance_to(app.xr_camera.global_position), 1.5), "Menu recenter independent of video")
	var native := NativeXrRendererManager.new(app)
	native.active = true
	native.renderer = RecordedRenderer.new()
	app.native_xr_renderer = native
	app.video_presentation = VideoPresentation.new(app.comp, native)
	check(sm.set_view_crop(0.1, 0.05), "Native framing enabled")
	native._sync_geometry()
	var frame: Dictionary = native.renderer.calls[-1]
	check(frame.top == 0.1 and frame.bottom == 0.05 and not frame.bezel, "Native receives atomic framing and geometry")
	check(is_equal_approx(frame.width / frame.height, (2048.0 / 1152.0) / sm.framing_height_fraction(s)), "Cropped shape matches visible per-eye ratio")
	check(sm.set_view_crop(0, 0), "Fit restores original")
	native._sync_geometry()
	check(is_equal_approx(s.mesh_size.x / s.mesh_size.y, 16.0 / 9.0), "Fit restores 16:9, never packed 32:9")
	for resolution in [Vector2i(3840, 1080), Vector2i(4096, 1152), Vector2i(5120, 1080)]:
		app.settings.host.pc_profile_resolution = resolution
		sm.set_view_crop(0.25, 0.25)
		native._sync_geometry()
		check(is_equal_approx(s.mesh_size.x / s.mesh_size.y, (float(resolution.x / 2) / resolution.y) / sm.framing_height_fraction(s)), "Quest 2/3 and ultrawide crop aspect " + str(resolution))
	app.settings.host.pc_profile_resolution = profile
	sm.set_view_crop(0, 0)
	sm.set_pointer_style(1.5, true)
	sm.set_keep_view_size(true)
	sm.save_view()
	sm.set_view_locked(true)
	app.state_manager.save_host_state()
	var stored := ConfigFile.new()
	check(stored.load("user://host_state.cfg") == OK, "Real config save succeeds")
	var values: Variant = stored.get_value("view-fixture", "quest_view_v1")
	sm.restore_view_options({})
	sm.restore_view_options(values)
	check(sm.view_locked and sm.keep_view_size and sm.menu_scale == 1.2 and sm.pointer_scale == 1.5 and sm.pointer_contrast and not sm.saved_view.is_empty(), "View preferences and preset survive disk round trip")
	stored.set_value("view-fixture", "quest_view_v1", {"version": 9, "future": "keep"})
	stored.set_value("unrelated-host", "private-value", "preserved")
	stored.save("user://host_state.cfg")
	app.state_manager.save_host_state()
	stored.load("user://host_state.cfg")
	check(stored.get_value("view-fixture", "quest_view_v1").version == 9 and stored.get_value("unrelated-host", "private-value") == "preserved", "Unknown future preferences and other hosts preserved")
	var host_after := ConfigFile.new()
	SettingsPersistence.write_host(host_after, "host", app.settings.host)
	check(host_after.encode_to_text() == stable_host, "All video, depth, color and transport preferences unchanged")
	check(app.settings_controller.restarts == 0, "No stream restart for local view controls")
	check(not app.controller_mapper.is_input_mapping_supported(), "Remote PC input stays disabled")
	native.active = false
	app.video_presentation = null
	app.native_xr_renderer = null
	native.renderer = null
	app.free()
	ui = null
	check(holding_ui.get_ref() == null, "Freeing menu releases PrecisionUI after repeat actions")
	print("display_view_settings ", "PASS" if failures.is_empty() else "FAIL", " checks=", checked)
	quit(0 if failures.is_empty() else 1)
