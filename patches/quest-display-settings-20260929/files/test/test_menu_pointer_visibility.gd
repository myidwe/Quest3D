extends SceneTree

# Exercise the production compositor setup and hit routing without starting
# OpenXR, a decoder, networking, or the application's startup sequence.
class QuietMain extends "res://main.gd":
	func _ready():
		pass
	func _process(_delta):
		pass
	func _notification(_what):
		pass

class AvailableEnvironment extends CompositionEnvironmentLayer:
	func setup(_scene: Node, _origin: Node3D, _angle: float, _fov: float) -> bool:
		# Only the platform capability check is replaced. All menu, keyboard and
		# cursor layers below are created by the actual production manager.
		return true

var failures: Array[String] = []
var presses := 0

func _init():
	_run.call_deferred()

func _check(condition: bool, message: String):
	if not condition:
		failures.append(message)
		printerr("FAIL: ", message)

func _pressed():
	presses += 1

func _aim(ray: RayCast3D, hit: Vector3):
	ray.global_position = Vector3.ZERO
	ray.target_position = ray.to_local(hit) * 1.2
	ray.force_raycast_update()

func _run():
	var app = load("res://main.tscn").instantiate()
	app.set_script(QuietMain)
	root.add_child(app)
	app.screen_registry.initialize(app.screen_mesh)
	app.screen_manager = ScreenManager.new(app)
	app.screen_mesh.position = Vector3(0, 0, -3)
	app.composition_environment = AvailableEnvironment.new()
	app.virtual_keyboard = VirtualKeyboard.new(app)
	app.add_child(app.virtual_keyboard)
	app.virtual_keyboard._setup_viewport("PointerTestKeyboardViewport")
	app.virtual_keyboard._setup_mesh("PointerTestKeyboardPanel")
	app.virtual_keyboard.hide()
	app.comp = CompositionLayerManager.new(app)
	app.comp.setup_background_equirect()
	app.comp.in_use = true
	app.video_presentation = VideoPresentation.new(app.comp, null)
	app.xr_interaction = XRInteraction.new(app)
	app.ui_visible = true
	app.ui_panel_3d.show()
	app.ui_panel_3d.position = Vector3(0, 0, -2)
	app.ui_panel_3d.set_meta(&"nf_role", &"panel")
	app.comp_ui.show()
	app.settings.pointer_steady = 0

	var ui = app.composition_panels.ui_layer
	var keyboard = app.composition_panels.keyboard_layer
	_check(ui != null and keyboard != null, "Production setup creates both panel layers")
	if ui == null or keyboard == null:
		app.free()
		quit(1)
		return
	for cursor in [app.comp_cursor, app.left_comp_cursor_layer]:
		_check(cursor.get_sort_order() > ui.get_sort_order(),
			str(cursor.name) + " must composite after the opaque settings panel")
		_check(cursor.get_sort_order() > keyboard.get_sort_order(),
			str(cursor.name) + " must composite after the keyboard, without equal-order ties")
		_check(cursor.get_alpha_blend(), "Cursor preserves transparent pixels")
		_check(not cursor.get_enable_hole_punch(), "Cursor cannot punch a hole through the panel")
		_check(cursor.get_layer_viewport().transparent_bg, "Cursor viewport background remains transparent")
		_check(cursor.get_child_count() == 0, "Visual cursor has no collision shape to intercept the input ray")

	# Real SubViewport/Button + the real panel collider reproduce the reported
	# condition: the button receives hover even when the old sort order hides
	# the pointer. This assertion alone is deliberately not considered enough.
	var button := Button.new()
	button.text = "Display"
	button.position = Vector2(480, 240)
	button.size = Vector2(240, 80)
	button.pressed.connect(_pressed)
	app.ui_viewport.add_child(button)
	var normal := StyleBoxFlat.new()
	var hover := StyleBoxFlat.new()
	hover.bg_color = Color(0.3, 0.4, 0.5)
	app.xr_interaction.populate_ui_buttons([{"btn": button, "norm": normal, "hover": hover}])
	var ray := RayCast3D.new()
	ray.collide_with_areas = true
	ray.collide_with_bodies = false
	ray.collision_mask = 2
	app.add_child(ray)
	app.mouse_raycast = ray
	await physics_frame
	await process_frame
	var pixel := button.get_global_rect().get_center()
	var target: Vector3 = app.ui_panel_3d.to_global(Vector3(
		(pixel.x / app._ui_viewport_size.x - 0.5) * app._ui_mesh_size.x,
		(0.5 - pixel.y / app._ui_viewport_size.y) * app._ui_mesh_size.y, 0))
	_aim(ray, target)
	_check(ray.is_colliding(), "Real ray hits the settings panel")
	if ray.is_colliding():
		_check(PointerTarget.resolve(ray.get_collider()).role == &"panel", "Hit remains a local UI target")
		app.xr_interaction._route_menu_pointer(ray.get_collision_point(), false, ray)
		app.xr_interaction._apply_ui_hover_states()
		_check(button.get_theme_stylebox("normal") == hover, "Real hit updates the button hover highlight")
		for mode in [0, 1]:
			app.settings.cursor_mode = mode
			app._update_cursor_layer()
			_check(app.comp_cursor.visible and app.comp_cursor.get_quad_size().x > 0.01,
				"Settings cursor is visible at normal size for cursor mode " + str(mode))
			_check(app.comp_cursor_viewport.get_node("CircleTexture").visible,
				"Both screen cursor modes use the settings contact dot")
			_check(not app.comp_cursor_viewport.get_node("PointerTexture").visible,
				"The screen arrow is not mistakenly drawn over the settings dot")
		app.xr_interaction._route_menu_pointer(ray.get_collision_point(), true, ray)
		app.xr_interaction._route_menu_pointer(ray.get_collision_point(), false, ray)
		await process_frame
		_check(presses == 1, "Layer order change preserves actual button activation")

	# Moving off and back onto the menu restores size instead of leaving a
	# microscopic hidden quad. It must reuse the existing viewport and layer.
	var cursor_id: int = app.comp_cursor.get_instance_id()
	var viewport_id: int = app.comp_cursor_viewport.get_instance_id()
	_aim(ray, Vector3(20, 20, -2))
	app._update_cursor_layer()
	_check(app.comp_cursor.get_quad_size().x < 0.001, "Pointer hides when there is no hit")
	_aim(ray, target)
	app._update_cursor_layer()
	_check(app.comp_cursor.get_quad_size().x > 0.01, "Pointer returns after leaving the panel")
	_check(app.comp_cursor.get_instance_id() == cursor_id and app.comp_cursor_viewport.get_instance_id() == viewport_id,
		"Pointer transitions do not allocate replacement layers or viewports")
	app._sync_interaction_viewports()
	_check(app.comp_cursor_viewport.render_target_update_mode == SubViewport.UPDATE_ALWAYS,
		"Primary pointer viewport updates while settings are open")
	_check(app.left_comp_cursor_viewport.render_target_update_mode == SubViewport.UPDATE_ALWAYS,
		"Secondary pointer viewport updates while settings are open")
	for scale_value in [0.85, 1.0, 1.2]:
		app.screen_manager.set_menu_scale(scale_value)
		_check(app.comp_ui.get_quad_size().is_equal_approx(app._ui_mesh_size * scale_value), "Menu compositor matches scaled collider")
		target = app.ui_panel_3d.to_global(Vector3((pixel.x / app._ui_viewport_size.x - 0.5) * app._ui_mesh_size.x, (0.5 - pixel.y / app._ui_viewport_size.y) * app._ui_mesh_size.y, 0))
		await physics_frame
		_aim(ray, target)
		_check(ray.is_colliding(), "Scaled panel remains a real ray target")
		if ray.is_colliding():
			var previous_presses := presses
			app.xr_interaction._route_menu_pointer(ray.get_collision_point(), true, ray)
			app.xr_interaction._route_menu_pointer(ray.get_collision_point(), false, ray)
			await process_frame
			_check(presses == previous_presses + 1, "Scaled menu click reaches original logical button")
	app.screen_manager.set_pointer_style(1.5, true)
	app._update_cursor_layer()
	_check(is_equal_approx(app.comp_cursor.get_quad_size().x, 0.035 * 1.5), "Pointer size reaches compositor")
	for vp in [app.comp_cursor_viewport, app.left_comp_cursor_viewport]:
		_check(vp.get_node("CircleTexture").material.get_shader_parameter("high_contrast") == true, "Both menu pointers receive contrast")
	_check(app.comp_cursor.get_instance_id() == cursor_id and app.comp_cursor_viewport.get_instance_id() == viewport_id, "Scale and contrast reuse cursor resources")
	app.ui_visible = false
	app._sync_interaction_viewports()
	_check(app.comp_cursor_viewport.render_target_update_mode == SubViewport.UPDATE_DISABLED,
		"Without menu or native video the primary pointer viewport sleeps")
	_check(app.left_comp_cursor_viewport.render_target_update_mode == SubViewport.UPDATE_DISABLED,
		"Without menu the secondary pointer viewport sleeps")
	app.free()
	if failures.is_empty():
		print("menu_pointer_visibility PASS")
		print("Production layer ordering, real ray/hover/click, both cursor styles, hide/return and viewport lifetime; headset photons unverified")
	else:
		printerr("Menu pointer visibility FAIL: ", failures.size(), " checks")
	quit(0 if failures.is_empty() else 1)
