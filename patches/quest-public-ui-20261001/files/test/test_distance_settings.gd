extends SceneTree

class QuietMain extends "res://main.gd":
	func _ready(): pass
	func _process(_delta): pass
	func _notification(_what): pass

class NoRestart extends SettingsController:
	var restarts := 0
	func _schedule_stream_restart(): restarts += 1

class RecordedRenderer extends RefCounted:
	var calls: Array[Dictionary] = []
	func supports_view_framing() -> bool: return true
	func set_geometry(pose, width, height, curve, radius, angle, order, bezel, top = 0.0, bottom = 0.0):
		calls.append({"pose": pose, "width": width, "height": height, "curve": curve,
			"radius": radius, "angle": angle, "order": order, "bezel": bezel, "top": top, "bottom": bottom})
	func set_compositor_sharpening(_value): pass

var failures: Array[String] = []
var checked := 0
func check(value: bool, message: String):
	checked += 1
	if not value:
		failures.append(message)
		printerr("REGRESSION FAIL ", message)

func _init(): _run.call_deferred()

func visible_center(call: Dictionary) -> Vector3:
	var pose: Transform3D = call.pose
	return pose.origin - pose.basis.z * call.radius if call.curve > 0 else pose.origin

func distance_slider_endpoint(sm: ScreenManager, native: NativeXrRendererManager, maximum: bool, label: String):
	var bounds := sm.view_distance_limits()
	var expected: float = bounds.y if maximum else bounds.x
	var slider := PrecisionDepthSlider.new()
	slider.minimum = bounds.x
	slider.maximum = bounds.y
	slider.step = 0.1
	slider.size = Vector2(320, 60)
	root.add_child(slider)
	var selected: Array[float] = []
	slider.depth_selected.connect(func(value):
		selected.append(value)
		check(sm.set_view_distance(value), label + " emitted distance accepted")
	)
	var event := InputEventMouseButton.new()
	event.button_index = MOUSE_BUTTON_LEFT
	event.position = Vector2(slider.size.x - 16 if maximum else 16, 30)
	event.pressed = true
	slider._input_depth(event)
	event.pressed = false
	slider._input_depth(event)
	check(selected.size() == 1 and selected[0] == expected, label + " exact non-grid slider endpoint emitted on release")
	check(is_equal_approx(sm.view_distance(), expected), label + " real ScreenManager endpoint applied")
	native._sync_geometry()
	var call: Dictionary = native.renderer.calls[-1]
	check(is_equal_approx(visible_center(call).distance_to(sm.main.xr_camera.global_position), expected), label + " endpoint reaches native pose")
	slider.free()

func _run():
	check(OS.get_environment("NIGHTFALL_FIRST_RUN_FIXTURE") == "1", "Isolated test profile required")
	if not failures.is_empty():
		quit(1)
		return
	var app = load("res://main.tscn").instantiate()
	app.set_script(QuietMain)
	root.add_child(app)
	app.screen_registry.initialize(app.screen_mesh)
	app.layout = ScreenLayout.single(Vector2i(4096, 1152))
	app.primary_screen.setup(app)
	app.primary_screen.create_corner_handles()
	app.primary_screen.apply_monitor(app.layout.monitors[0], app.layout.frame_size)
	app.comp = CompositionLayerManager.new(app)
	app.screen_manager = ScreenManager.new(app)
	app.settings_controller = NoRestart.new(app)
	app.state_manager = StateManager.new(app)
	app.xr_camera = XRCamera3D.new()
	app.add_child(app.xr_camera)
	app.settings.host.pc_profile_resolution = Vector2i(4096, 1152)
	app.get_node("%IPInput").text = "distance-fixture"
	var sm: ScreenManager = app.screen_manager
	var screen: VRScreen = app.primary_screen
	screen.mesh_size = Vector2(2.4, 1.35)
	screen.position = Vector3(0, 0, -3)
	screen.curvature = 0
	var native := NativeXrRendererManager.new(app)
	native.active = true
	native.renderer = RecordedRenderer.new()
	app.native_xr_renderer = native
	app.video_presentation = VideoPresentation.new(app.comp, native)
	app.comp.in_use = false
	check(not sm.keep_view_size and sm.level_lock, "Existing fixed-width and level defaults preserved")
	check(sm.view_distance_limits().is_equal_approx(Vector2(0.5, 10)), "Flat fixed-width exposes full reachable metres")
	var host_before := ConfigFile.new()
	SettingsPersistence.write_host(host_before, "host", app.settings.host)
	var transport_before := host_before.encode_to_text()
	check(sm.set_view_distance(0.5), "Nearest flat endpoint accepted")
	native._sync_geometry()
	var near: Dictionary = native.renderer.calls[-1]
	check(visible_center(near).is_equal_approx(screen.global_position) and is_equal_approx(visible_center(near).distance_to(app.xr_camera.global_position), 0.5), "Native receives actual nearest pose")
	check(is_equal_approx(near.width, 2.4), "Near keeps physical width")
	check(sm.set_view_distance(10), "Farthest flat endpoint accepted")
	native._sync_geometry()
	var far: Dictionary = native.renderer.calls[-1]
	check(visible_center(far).is_equal_approx(screen.global_position) and is_equal_approx(visible_center(far).distance_to(app.xr_camera.global_position), 10), "Native receives actual farthest pose")
	check(is_equal_approx(far.width, near.width), "Far keeps physical width")
	check(is_equal_approx((near.width / 0.5) / (far.width / 10), 20), "Projected half-angle tangent differs by 20 across flat endpoints")
	check(2 * atan(near.width / 1.0) > 4 * atan(far.width / 20.0), "Actual angular extent clearly changes with apparent-size toggle off")
	check(sm.move_primary_screen(-ScreenManager.DISTANCE_BUTTON_STEP) and is_equal_approx(sm.view_distance(), 9.75), "Distance button step is 25 cm")
	check(sm.set_view_distance(-10) and is_equal_approx(sm.view_distance(), 0.5), "Low out-of-range value clamps to exposed endpoint")
	check(sm.set_view_distance(40) and is_equal_approx(sm.view_distance(), 10), "High out-of-range value clamps to exposed endpoint")
	check(not sm.set_view_distance(NAN) and not sm.set_view_distance(INF), "Nonfinite distance rejected")
	screen.curvature = 2
	screen.position = Vector3(0.1, 0.1, -3)
	check(sm.set_view_distance(0.5), "Off-axis nearest endpoint accepted")
	native._sync_geometry()
	var off_axis: Dictionary = native.renderer.calls[-1]
	check(is_equal_approx(off_axis.radius, 1.0) and is_equal_approx(off_axis.angle, 2.4), "Rounded off-axis 0.5m endpoint cannot jump cylinder radius to 6m")
	check(is_equal_approx(visible_center(off_axis).distance_to(app.xr_camera.global_position), 0.5), "Off-axis native visible center remains at nearest endpoint")
	native.active = false
	app.comp.in_use = true
	check(is_equal_approx(screen.get_cylinder_radius(), off_axis.radius), "Off-axis near legacy and native radius remain identical")
	native.active = true
	app.comp.in_use = false
	for curve in [0, 1, 2]:
		screen.curvature = curve
		screen.mesh_size = Vector2(8, 4.5)
		screen.position = Vector3(0, 0, -3)
		sm.keep_view_size = false
		var limits := sm.view_distance_limits()
		check(limits.y == 10 and limits.x >= 0.5, "Exposed native distance bounds are valid for curve " + str(curve))
		check(sm.set_view_distance(limits.x), "Nearest reachable native curved endpoint accepted " + str(curve))
		native._sync_geometry()
		var call: Dictionary = native.renderer.calls[-1]
		check(visible_center(call).is_equal_approx(screen.global_position), "Native cylinder center converts back to actual screen pose " + str(curve))
		check(is_equal_approx(call.width, 8) and is_equal_approx(call.width / call.height, 16.0 / 9.0), "Curvature preserves width and per-eye aspect " + str(curve))
		if curve > 0:
			check(call.angle <= PI + 0.0001, "Nearest endpoint keeps cylinder below half-turn " + str(curve))
			var radius_before := screen.get_cylinder_radius()
			# Native does not need the legacy in_use flag. Fallback preserves the
			# same geometry instead of changing to legacy's fixed mesh radius.
			native.active = false
			app.comp.in_use = true
			check(is_equal_approx(screen.get_cylinder_radius(), radius_before), "Native to legacy composition radius stays identical " + str(curve))
			native.active = true
			app.comp.in_use = false
		screen.position = Vector3(0, 0, -3)
		screen.mesh_size = Vector2(4, 2.25)
		sm.keep_view_size = true
		limits = sm.view_distance_limits()
		var ratio := screen.mesh_size.x / sm.view_distance()
		check(sm.set_view_distance(limits.x), "Apparent-size nearest endpoint accepted " + str(curve))
		native._sync_geometry()
		var angle_before: float = native.renderer.calls[-1].angle
		check(is_equal_approx(screen.mesh_size.x / sm.view_distance(), ratio), "Explicit apparent-size mode keeps width/distance at near " + str(curve))
		check(sm.set_view_distance(sm.view_distance_limits().y), "Apparent-size farthest endpoint accepted " + str(curve))
		native._sync_geometry()
		check(is_equal_approx(screen.mesh_size.x / sm.view_distance(), ratio), "Explicit apparent-size mode keeps width/distance at far " + str(curve))
		if curve > 0:
			check(is_equal_approx(native.renderer.calls[-1].angle, angle_before), "Native cylinder arc stays constant in apparent-size mode " + str(curve))
	check(is_equal_approx(sm.view_distance_limits().y, 9), "12m width cap narrows apparent-size distance slider accurately")
	screen.curvature = 2
	screen.mesh_size = Vector2(8, 4.5)
	screen.position = Vector3(0, 0, -3)
	sm.keep_view_size = false
	distance_slider_endpoint(sm, native, false, "Curved 8m width minimum")
	screen.mesh_size = Vector2(4.1, 4.1 / (16.0 / 9.0))
	screen.position = Vector3(0, 0, -3)
	sm.keep_view_size = true
	distance_slider_endpoint(sm, native, true, "Apparent-size 4.1m width maximum")
	native.active = false
	app.comp.in_use = false
	check(sm.view_distance_limits() == Vector2.ZERO and not sm.set_view_distance(4), "Unsupported curved mesh apparent-size mode cannot expose phantom controls")
	native.active = true
	sm.restore_view_options({"version": 1, "keep_size": true, "level_lock": false})
	check(sm.keep_view_size and not sm.level_lock and sm.view_options().version == 1, "Explicit saved keep-size and free alignment preserved without migration")
	sm.restore_view_options({"version": 1, "keep_size": false, "level_lock": true})
	check(not sm.keep_view_size and sm.level_lock, "Explicit fixed-width and Level settings preserved")
	var pose_before := screen.global_transform
	sm.view_locked = true
	check(not sm.set_view_distance(4) and screen.global_transform == pose_before, "Locked distance leaves geometry untouched")
	sm.view_locked = false
	var host_after := ConfigFile.new()
	SettingsPersistence.write_host(host_after, "host", app.settings.host)
	check(host_after.encode_to_text() == transport_before, "Distance controls leave codec, depth, video and transport unchanged")
	check(app.settings_controller.restarts == 0, "Distance controls schedule no stream restart")
	native.active = false
	app.video_presentation = null
	app.native_xr_renderer = null
	native.renderer = null
	app.free()
	print("distance_settings ", "PASS" if failures.is_empty() else "FAIL", " checks=", checked)
	quit(0 if failures.is_empty() else 1)
