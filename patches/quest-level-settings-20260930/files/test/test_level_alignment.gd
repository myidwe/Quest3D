extends SceneTree

class QuietMain extends "res://main.gd":
	func _ready(): pass
	func _process(_delta): pass
	func _notification(_what): pass

class NoRestart extends SettingsController:
	var restarts := 0
	func _schedule_stream_restart(): restarts += 1

class HeldPointer extends XRInteraction:
	var held := true
	func _is_now_clicking() -> bool: return held

var checks := 0
var failures: Array[String] = []
var app

func _init(): _run.call_deferred()

func check(value: bool, message: String):
	checks += 1
	if not value:
		failures.append(message)
		printerr("REGRESSION FAIL ", message)

func upright(node: Node3D) -> bool:
	return node.global_basis.orthonormalized().y.is_equal_approx(Vector3.UP)

func click(ui, name: String):
	var button: Button = ui.view_panels.Position.get_node(name)
	check(button.is_visible_in_tree() and not button.disabled, name + " enabled and reachable")
	if not button.disabled:
		button.button_down.emit()
		button.button_up.emit()

func _run():
	check(OS.get_environment("NIGHTFALL_FIRST_RUN_FIXTURE") == "1", "Isolated profile required")
	if not failures.is_empty():
		quit(1)
		return
	if FileAccess.file_exists("user://host_state.cfg"):
		check(DirAccess.remove_absolute("user://host_state.cfg") == OK, "Reset isolated fixture only")
	app = load("res://main.tscn").instantiate()
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
	app.comp = CompositionLayerManager.new(app)
	app.video_presentation = VideoPresentation.new(app.comp, null)
	app.xr_interaction = HeldPointer.new(app)
	app.xr_camera = XRCamera3D.new()
	app.add_child(app.xr_camera)
	app.hand_raycast = RayCast3D.new()
	app.add_child(app.hand_raycast)
	app.ui_controller = UIController.new(app)
	app.settings.host.pc_profile_resolution = Vector2i(4096, 1152)
	app.primary_screen.mesh_size = Vector2(2.4, 1.35)
	app.primary_screen.position = Vector3(0, 0, -3)
	app.primary_screen.curvature = 0
	app.ui_visible = true
	app.ui_panel_3d.show()
	app.get_node("%IPInput").text = "level-fixture"
	app.ui_controller.build_ui()
	await process_frame
	await process_frame
	var sm: ScreenManager = app.screen_manager
	var screen: VRScreen = app.primary_screen
	var menu: Node3D = app.ui_panel_3d
	var ui: PrecisionUI = app.ui_controller.precision
	var before := ConfigFile.new()
	SettingsPersistence.write_host(before, "host", app.settings.host)
	var host_before := before.encode_to_text()
	check(sm.level_lock, "Existing profiles default to Level")
	# Real head poses: yaw must follow gaze while pitch and roll must not.
	for yaw in [-2.0, 0.0, 1.3]:
		for pitch in [-0.8, 0.0, 0.7]:
			for roll in [-0.6, 0.0, 0.5]:
				app.xr_camera.rotation = Vector3(pitch, yaw, roll)
				app.xr_camera.position = Vector3(0.3, 1.6, 0.2)
				var distance := sm.view_distance()
				var size := screen.mesh_size
				check(sm.center_view(), "Center accepted")
				check(upright(screen) and absf(screen.position.y - app.xr_camera.position.y) < 0.0001, "Head tilt cannot tilt/raise centred view")
				check(screen.global_basis.is_equal_approx(Basis(Vector3.UP, yaw)), "Center follows horizontal heading")
				check(is_equal_approx(sm.view_distance(), distance) and screen.mesh_size == size, "Center preserves distance and size")
				var position := screen.global_position
				check(sm.shift_view(Vector2(0, 0.1)), "Move up accepted")
				check(screen.global_position.is_equal_approx(position + Vector3.UP * 0.1), "Move up uses world up even with head roll")
	# Singular gaze retains the last valid heading and a finite screen pose.
	app.xr_camera.rotation = Vector3(0.2, 1.1, 0.4)
	sm.center_view()
	var heading := screen.global_basis
	for pitch in [PI / 2, -PI / 2]:
		app.xr_camera.rotation = Vector3(pitch, -2.2, 0.5)
		sm.center_view()
		check(screen.global_basis.is_equal_approx(heading) and screen.global_position.is_finite(), "Vertical gaze retains reliable heading")
	ui._show_view_panel("Position")
	check(ui.view_controls.tilt_plus.disabled and ui.view_controls.roll_plus.disabled, "Rotation visibly disabled in Level")
	var pose := screen.global_transform
	check(not sm.tilt_view(2) and not sm.roll_view(2) and screen.global_transform == pose, "API also blocks accidental Level rotation")
	click(ui, "AlignFree")
	check(not sm.level_lock and not ui.view_controls.tilt_plus.disabled, "Free explicitly enables rotation")
	click(ui, "TiltPlus")
	click(ui, "RollPlus")
	check(screen.global_position == pose.origin and not upright(screen), "Manual Tilt/Roll change orientation only")
	app.xr_camera.rotation = Vector3(0.6, 0.4, 0.35)
	sm.center_view()
	check(screen.global_basis.is_equal_approx(app.xr_camera.global_basis), "Free Center follows full head pose")
	var free_position := screen.global_position
	click(ui, "AlignLevel")
	check(upright(screen) and screen.global_position == free_position, "Selecting Level straightens without translating")
	check(sm.move_primary_screen(0.1) and upright(screen), "Distance changes retain Level")
	sm.set_view_locked(true)
	pose = screen.global_transform
	check(not sm.set_level_lock(false) and not sm.roll_view(2) and not sm.apply_view_preset("Reclined"), "Lock prevents alignment/preset bypass")
	check(screen.global_transform == pose and sm.level_lock, "Lock preserves exact pose and policy")
	sm.set_view_locked(false)
	# Saved views have a heading-relative reference independent of head tilt.
	sm.center_view()
	sm.shift_view(Vector2(0.2, 0.1))
	check(sm.save_view(), "Save level view")
	var saved := sm.saved_view.duplicate(true)
	check(saved.alignment == "level" and saved.has("level_position"), "Preset records explicit reference with legacy fields")
	app.xr_camera.position += Vector3(0.6, 0.2, -0.3)
	app.xr_camera.rotation = Vector3(-0.5, -0.8, -0.6)
	check(sm.apply_view_preset("My view") and upright(screen), "Saved Level ignores new head pitch/roll")
	var relative := sm.placement_frame().affine_inverse() * screen.global_transform
	check(relative.origin.is_equal_approx(Vector3(saved.level_position[0], saved.level_position[1], saved.level_position[2])), "Saved position follows new heading, not head inclination")
	check(sm.apply_view_preset("Reclined") and not sm.level_lock and screen.global_basis.is_equal_approx(app.xr_camera.global_basis), "Reclined explicitly opts into Free")
	sm.save_view()
	var free_saved := sm.saved_view.duplicate(true)
	check(sm.apply_view_preset("Standard") and sm.level_lock and upright(screen), "Standard returns to Level")
	check(sm.apply_view_preset("My view") and not sm.level_lock, "Saved Free view retains explicit policy")
	check(sm.apply_view_preset("Cinema") and sm.level_lock and upright(screen), "Cinema returns to Level")
	saved.erase("alignment")
	saved.erase("level_position")
	saved.erase("level_rotation")
	sm.saved_view = saved
	check(sm.valid_saved_view(saved) and sm.apply_view_preset("My view") and upright(screen), "Legacy preset remains loadable under default Level")
	for invalid in [{"alignment": "unexpected"}, {"alignment": "level", "level_position": [0, 0, -3]}, {"alignment": "level", "level_position": [0, 0, -3], "level_rotation": [0, INF, 0]}]:
		var bad := saved.duplicate(true)
		bad.merge(invalid, true)
		check(not sm.valid_saved_view(bad), "Malformed alignment fields rejected")
	for invalid in [{}, {"version": 1, "level_lock": "false"}, {"version": 2, "level_lock": false}]:
		sm.restore_view_options(invalid)
		check(sm.level_lock, "Missing/malformed/future preferences default Level")
	# Both menu placements retain pixel scale and video geometry.
	sm.set_menu_scale(1.2)
	pose = screen.global_transform
	sm.center_menu()
	check(upright(menu) and is_equal_approx(menu.position.y, app.xr_camera.position.y), "Menu Center uses horizontal gaze")
	check(menu.global_basis.get_scale().is_equal_approx(Vector3.ONE * 1.2) and screen.global_transform == pose, "Menu Center preserves menu scale and video")
	app.is_xr_active = true
	app._ui_saved_rot_x = 0.45
	app._set_ui_position()
	check(upright(menu), "Reopening a formerly tilted menu stays Level")
	app._set_ui_visible(false)
	app._set_ui_visible(true)
	check(upright(menu), "Visibility path also preserves Level")
	app._reposition_screen_and_ui()
	check(upright(screen) and is_equal_approx(screen.position.y, app.xr_camera.position.y), "Reset uses horizontal reference")
	# Exercise production grab with a pitched controller, rather than calling
	# only orientation helpers. Free preserves a deliberately selected roll.
	for is_level in [true, false]:
		sm.set_level_lock(is_level)
		for node in [screen, menu]:
			node.rotation = Vector3(0.3, 0.1, 0.2)
			app.hand_raycast.position = Vector3(0.05, 0.02, -0.01)
			app.hand_raycast.rotation = Vector3(0.6, 0.2, 0.1)
			app.grabbed_node = node
			app.grab_start_hand_pos = Vector3.ZERO
			app.grab_forward = Vector3.FORWARD
			app.grab_start_hand_basis = Basis.from_euler(Vector3(0.1, 0.2, 0))
			app.grab_start_node_pos = node.global_position
			app.grab_start_node_euler = node.rotation
			app.xr_interaction.held = true
			var start: Vector3 = node.global_position
			var scale_value: Vector3 = node.global_basis.get_scale()
			app.xr_interaction.handle_grab()
			check(node.global_position.is_equal_approx(start + Vector3(0.3, 0.12, -0.12)), "Grab translation remains functional")
			check(upright(node) if is_level else absf(node.rotation.z - 0.2) < 0.0001, "Grab obeys Level/explicit Free roll")
			check(node.global_basis.get_scale().is_equal_approx(scale_value), "Grab preserves panel scale")
			app.xr_interaction.held = false
			app.xr_interaction.handle_grab()
			check(app.grabbed_node == null, "Grab release ends cleanly")
	app.is_xr_active = false
	# Real disk round trip, including old tilted screen placements.
	sm.saved_view = free_saved
	app.state_manager.save_host_state()
	sm.restore_view_options({})
	app.state_manager.load_host_state("level-fixture")
	check(not sm.level_lock and not upright(screen), "Saved Free pose and policy survive full host reload")
	var stored := ConfigFile.new()
	check(stored.load("user://host_state.cfg") == OK, "Config readable")
	var options: Dictionary = stored.get_value("level-fixture", "quest_view_v1")
	options.erase("level_lock")
	stored.set_value("level-fixture", "quest_view_v1", options)
	stored.save("user://host_state.cfg")
	app.state_manager.load_host_state("level-fixture")
	check(sm.level_lock and upright(screen), "Legacy tilted placement loads upright by default")
	sm.set_level_lock(false)
	app.get_node("%IPInput").text = "new-host"
	app.state_manager.load_host_state("new-host")
	check(sm.level_lock and upright(screen), "Unsaved host never inherits previous Free policy or tilt")
	var after := ConfigFile.new()
	SettingsPersistence.write_host(after, "host", app.settings.host)
	check(after.encode_to_text() == host_before, "View controls leave depth/color/codec preferences unchanged")
	check(app.settings_controller.restarts == 0, "No stream restart introduced")
	app.free()
	print("LEVEL_ALIGNMENT ", "PASS" if failures.is_empty() else "FAIL", " checks=", checks)
	quit(0 if failures.is_empty() else 1)
