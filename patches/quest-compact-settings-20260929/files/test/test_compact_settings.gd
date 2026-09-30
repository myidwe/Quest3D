extends SceneTree

# Full production UI and real panel collider, with networking/startup omitted.
class QuietMain extends "res://main.gd":
	func _ready(): pass
	func _process(_delta): pass
	func _notification(_what): pass

class RecordedState extends StateManager:
	var saves := 0
	func save_state(): saves += 1

var app
var ray: RayCast3D
var checks := 0
var pages_checked := 0

func _init(): _run.call_deferred()

func check(condition: bool, message: String):
	checks += 1
	assert(condition, message)

func hit(pixel: Vector2) -> Vector3:
	return app.ui_panel_3d.to_global(Vector3(
		(pixel.x / app._ui_viewport_size.x - 0.5) * app._ui_mesh_size.x,
		(0.5 - pixel.y / app._ui_viewport_size.y) * app._ui_mesh_size.y, 0))

func click(button: Button):
	check(button.is_visible_in_tree() and not button.disabled, "Navigation is available: " + str(button.name))
	var target := hit(button.get_global_rect().get_center())
	app.xr_interaction._route_menu_pointer(target, true, ray)
	await process_frame
	app.xr_interaction._route_menu_pointer(target, false, ray)
	await process_frame

func inspect_page():
	pages_checked += 1
	var visible: Array[Button] = []
	var root: Control = app.get_node("%UIRoot")
	var bounds := Rect2(Vector2.ZERO, app._ui_viewport_size)
	var footer: Control = app.ui_viewport.find_child("CompGrabBar", true, false)
	var content_bounds := Rect2(0, 0, 840, footer.get_global_rect().position.y)
	for node in root.find_children("*", "Control", true, false):
		if not node.is_visible_in_tree(): continue
		if node is Label or node is Button:
			check(bounds.encloses(node.get_global_rect()), "Visible control remains in panel: " + str(node.get_path()))
		if node is Label:
			check(node.get_theme_font_size("font_size") >= 18, "Readable helper text: " + str(node.get_path()))
		if node is Button:
			check(content_bounds.encloses(node.get_global_rect()), "Button never overlaps fixed drag footer: " + str(node.name))
			check(node.size.x >= 48 and node.size.y >= 48, "Controller target has useful size: " + str(node.name))
			visible.append(node)
	for i in visible.size():
		for j in range(i + 1, visible.size()):
			check(not visible[i].get_global_rect().intersects(visible[j].get_global_rect()), "No overlapping interactive targets: %s / %s" % [visible[i].name, visible[j].name])
		# Verify actual physics hit and inverse coordinate mapping at centre plus
		# opposite inset corners, including controls at the new panel extremities.
		var rect := visible[i].get_global_rect()
		for pixel in [rect.get_center(), rect.position + Vector2(8, 8), rect.end - Vector2(8, 8)]:
			var target := hit(pixel)
			ray.global_position = app.xr_camera.global_position
			ray.target_position = ray.to_local(target) * 1.05
			ray.force_raycast_update()
			check(ray.is_colliding(), "Physics target reachable: " + str(visible[i].name))
			check(app.xr_interaction._menu_pixel_from_hit(target).is_equal_approx(pixel), "Visible and logical target agree: " + str(visible[i].name))
	check(footer.get_global_rect().size.x >= 300 and footer.size.y >= 48, "Bottom handle remains easy to grab")

func _run():
	app = load("res://main.tscn").instantiate()
	app.set_script(QuietMain)
	root.add_child(app)
	app.screen_mesh.setup(app)
	app.screen_mesh.curvature = 0
	app.screen_mesh.create_corner_handles()
	app.screen_registry.initialize(app.screen_mesh)
	app.layout = ScreenLayout.new()
	app.screen_manager = ScreenManager.new(app)
	app.settings_controller = SettingsController.new(app)
	app.controller_mapper = ControllerMapper.new(app)
	app.add_child(app.controller_mapper)
	app.state_manager = RecordedState.new(app)
	app.pc_control = PcControl.new(app)
	app.xr_interaction = XRInteraction.new(app)
	app.xr_camera = XRCamera3D.new()
	app.add_child(app.xr_camera)
	app.ui_controller = UIController.new(app)
	app.settings.host.pc_profile_resolution = Vector2i(4096, 1152)
	app.ui_panel_3d.position = Vector3(0.1, 0.1, -1.5)
	app.ui_panel_3d.rotation = Vector3(0.06, 0.1, 0.0)
	app.ui_visible = true
	app.ui_panel_3d.show()
	ray = RayCast3D.new()
	ray.collide_with_areas = true
	ray.collide_with_bodies = false
	ray.collision_mask = 2
	app.add_child(ray)
	app.mouse_raycast = ray
	app.ui_controller.build_ui()
	await process_frame
	await physics_frame
	check(app.ui_viewport.size == Vector2i(840, 820), "Compact nearly square viewport")
	check(app.ui_viewport.size.x * app.ui_viewport.size.y <= 1200 * 580, "Settings texture does not grow")
	check((Vector2(app.ui_viewport.size) / app._ui_mesh_size).is_equal_approx(Vector2(1000, 1000)), "Original pixel density and non-stretched geometry")
	check(app.ui_panel_3d.mesh.size == app._ui_mesh_size, "Actual scene mesh uses compact aspect")
	var precision: PrecisionUI = app.ui_controller.precision
	var current_width: float = app.primary_screen.mesh_size.x
	for scale_value in [0.85, 1.0, 1.2]:
		app.screen_manager.set_menu_scale(scale_value)
		await physics_frame
		for tab in [0, 3, 4, 1, 2, 6]:
			app.ui_controller.switch_tab(tab)
			await process_frame
			inspect_page()
			for child in precision.pages[tab].get_children():
				if child is Button and child.has_meta("precision_subpage"):
					await click(child)
					inspect_page()
		app.ui_controller.switch_tab(0)
		for panel in ["Position", "Views", "Environment", "Framing"]:
			precision._show_view_panel("Overview")
			await process_frame
			await click(precision.view_panels.Overview.get_node(panel + "Open"))
			check(precision.view_panels[panel].is_visible_in_tree(), "Real ray opens " + panel)
			inspect_page()
			await click(precision.view_panels[panel].get_node(panel + "Back"))
			check(precision.view_panels.Overview.is_visible_in_tree(), "Real ray returns to Display")
	check(app.primary_screen.mesh_size.x == current_width, "Browsing and menu scale never resize the video")
	check(not app.controller_mapper.active, "Navigation never enables excluded PC input")
	# Long host names and status messages must not widen the shell or shift its footer.
	app.ui_controller.switch_tab(1)
	app._ui_host_label.text = "PC-" + "LongHostName".repeat(8)
	app._ui_status_label.text = "연결 상태와 실제 영상 수신 정보를 확인하는 중 ".repeat(10)
	await process_frame
	inspect_page()
	check(app.ui_viewport.find_child("Panel", true, false).size == Vector2(840, 820), "Dynamic text cannot widen the panel")
	# The stream information shown under Sharpness is also available in Stream.
	app.ui_controller._stream_resolution_note.text = "Eye 2048×1152 · 16:9 · Native"
	precision.refresh_labels()
	check(precision._stream_info.text == app.ui_controller._stream_resolution_note.text, "Stream reports the same observed eye information")
	# Tint's paired actions retain the original settings-controller bindings,
	# step size, limits and reset scope while the value is shown only once.
	app.ui_controller.switch_tab(4)
	for button in precision.pages[4].get_children():
		if button is Button and button.get_meta("precision_subpage", -1) == 2:
			await click(button)
	var warmer: Button = app.ui_viewport.find_child("ColorWarmer", true, false)
	var cooler: Button = app.ui_viewport.find_child("ColorCooler", true, false)
	var magenta: Button = app.ui_viewport.find_child("ColorMagenta", true, false)
	var reset: Button = app.ui_viewport.find_child("ColorReset", true, false)
	var old_temperature: int = app.settings.host.picture_temperature
	await click(warmer)
	check(app.settings.host.picture_temperature == old_temperature + PictureColor.STEP, "Actual warmer action keeps original step")
	check(precision._tint_values[0].text == "%+d" % app.settings.host.picture_temperature and warmer.text == "따뜻하게", "Immediate readout and action caption remain distinct")
	await click(cooler)
	check(app.settings.host.picture_temperature == old_temperature, "Paired temperature action reverses")
	await click(magenta)
	check(app.settings.host.picture_tint == PictureColor.STEP, "Tint action changes tint only")
	app.settings.brightness_pct = 10
	await click(reset)
	check(app.settings.host.picture_temperature == 0 and app.settings.host.picture_tint == 0 and app.settings.brightness_pct == 10, "Reset preserves brightness and resets only temperature/tint")
	app.settings.host.picture_temperature = PictureColor.LIMIT
	app.settings_controller.refresh_color_balance_controls()
	precision.refresh_labels()
	check(warmer.disabled and not cooler.disabled, "Limit disables only the exhausted direction")
	inspect_page()
	app.free()
	print("COMPACT_SETTINGS PASS pages=", pages_checked, " checks=", checks, " real ray navigation, target bounds and preserved video settings")
	quit()
