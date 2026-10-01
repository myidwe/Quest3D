extends SceneTree

class QuietMain extends "res://main.gd":
	var ordinary_clicks := 0
	func _ready():
		pass
	func _process(_delta):
		pass
	func _notification(_what):
		pass
	func exit_app():
		ordinary_clicks += 1

class RecordedState extends StateManager:
	var saves := 0
	func save_state():
		saves += 1

class ControlledPointer extends XRInteraction:
	var held := false
	var click_events: Array = []
	func _is_now_clicking() -> bool:
		return held
	func _push_ui_click(pos: Vector2, pressed: bool):
		click_events.append({"position": pos, "pressed": pressed})
		super._push_ui_click(pos, pressed)

class ObservedBackend extends StreamBackend:
	func get_video_output_info() -> Dictionary:
		return {"observed": true, "valid": true, "codec_instance": 11, "codec_generation": 2, "width": 3840, "height": 1080, "crop_left": 0, "crop_top": 0, "crop_right": 3839, "crop_bottom": 1079, "transport_epoch": "a".repeat(32), "decoder_epoch": 7}

class ObservedRenderer extends Object:
	func get_last_successful_submission() -> Dictionary:
		return {"transport_epoch": "a".repeat(32), "decoder_epoch": 7, "stage": "xr_end_frame_succeeded", "xr_end_frame_verified": true, "photon_verified": false, "eyes": [{"image_rect": Rect2i(0, 0, 1920, 1080), "content_rect_in_image": Rect2i(0, 0, 1920, 1080)}, {"image_rect": Rect2i(1920, 0, 1920, 1080), "content_rect_in_image": Rect2i(1920, 0, 1920, 1080)}]}

func _init():
	_run.call_deferred()

func _hit(app, pixel: Vector2) -> Vector3:
	var local := Vector3((pixel.x / app._ui_viewport_size.x - 0.5) * app._ui_mesh_size.x,
		(0.5 - pixel.y / app._ui_viewport_size.y) * app._ui_mesh_size.y, 0)
	return app.ui_panel_3d.to_global(local)

func _changed_pixels(before: Image, after: Image, area: Rect2) -> int:
	var count := 0
	var bounds := Rect2i(area).intersection(Rect2i(0, 0, before.get_width(), before.get_height()))
	for y in range(bounds.position.y, bounds.end.y):
		for x in range(bounds.position.x, bounds.end.x):
			var delta := before.get_pixel(x, y) - after.get_pixel(x, y)
			if absf(delta.r) + absf(delta.g) + absf(delta.b) > 0.02: count += 1
	return count

func _run():
	# Instantiate the real complete menu scene, without startup/network.
	var app = load("res://main.tscn").instantiate()
	app.set_script(QuietMain)
	root.add_child(app)
	app.screen_registry.initialize(app.screen_mesh)
	app.screen_mesh.setup(app)
	app.screen_mesh.curvature = 0
	app.screen_mesh.create_corner_handles()
	app.layout = ScreenLayout.new()
	app.screen_manager = ScreenManager.new(app)
	app.settings_controller = SettingsController.new(app)
	app.controller_mapper = ControllerMapper.new(app)
	app.add_child(app.controller_mapper)
	assert(not app.controller_mapper.is_input_mapping_supported(), "Actual compiled PC build blocks input mapping")
	assert(not app.controller_mapper.active)
	print("PC_INPUT_POLICY disabled")
	var state := RecordedState.new(app)
	app.state_manager = state
	app.pc_control = PcControl.new(app)
	app.ui_controller = UIController.new(app)
	var pointer := ControlledPointer.new(app)
	app.xr_interaction = pointer
	app.mouse_raycast = RayCast3D.new()
	app.add_child(app.mouse_raycast)
	app.xr_camera = XRCamera3D.new()
	app.add_child(app.xr_camera)
	app.ui_visible = true
	app.ui_panel_3d.visible = true
	app.ui_panel_3d.position = Vector3(0.3, 0.4, -2.0)
	app.ui_panel_3d.rotation.y = 0.25
	app.settings.host.pc_profile_resolution = Vector2i(3840, 1080)
	app.ui_controller.build_ui()
	await process_frame
	await process_frame
	app.ui_viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	app._last_hostname = "MY PC"
	app.session_lifecycle.stream_started()
	app.stream_backend = ObservedBackend.new(null)
	app.native_xr_renderer = NativeXrRendererManager.new(app)
	app.native_xr_renderer.renderer = ObservedRenderer.new()
	app.native_xr_renderer.active = true
	app._ui_host_label.text = "MY PC · 연결됨"
	app.ui_controller.set_disconnect_visible(true)
	var pc = app.pc_control
	pc._host = 7
	var live := {"version": 1, "session_id": "fixture", "requested_mode": "3d", "effective_mode": "3d", "revision": 12, "last_seq": 2, "eye_width": 1920, "disparity": 22.32, "publisher_age_ms": 10, "applied_request": null, "rejected_request": null, "fallback_reason": null}
	pc._on_status(200, live, "", pc._generation)
	for tab_index in [0, 3, 4, 1, 2, 6]:
		app.ui_controller.switch_tab(tab_index)
		await process_frame
		await process_frame
		RenderingServer.force_draw(false)
		app.ui_viewport.get_texture().get_image().save_png("../after-tab-%d.png" % tab_index)
		for child in app.ui_controller.precision.pages[tab_index].get_children():
			if child is Button and child.has_meta("precision_subpage"):
				child.button_down.emit()
				await process_frame
				await process_frame
				RenderingServer.force_draw(false)
				app.ui_viewport.get_texture().get_image().save_png("../after-tab-%d-sub-%d.png" % [tab_index, child.get_meta("precision_subpage")])
		print("RENDER ", tab_index)
	app.ui_controller.switch_tab(0)
	for name in app.ui_controller.precision.view_panels:
		app.ui_controller.precision._show_view_panel(name)
		await process_frame
		await process_frame
		RenderingServer.force_draw(false)
		app.ui_viewport.get_texture().get_image().save_png("../after-view-%s.png" % name)
	app.screen_manager.set_level_lock(false)
	app.ui_controller.precision._show_view_panel("Position")
	await process_frame
	await process_frame
	RenderingServer.force_draw(false)
	app.ui_viewport.get_texture().get_image().save_png("../after-view-Position-Free.png")
	var visible_icons: Image = app.ui_viewport.get_texture().get_image()
	var icon_buttons: Array[Button] = []
	var icon_textures: Array[Texture2D] = []
	for key in ["PositionBack", "DistanceMinus", "DistancePlus", "Move0", "Move1", "Move2", "Move3", "TiltMinus", "TiltPlus", "RollMinus", "RollPlus"]:
		var button: Button = app.ui_controller.precision.view_panels.Position.get_node(key)
		assert(button.icon != null, key + " resource loaded")
		icon_buttons.append(button)
		icon_textures.append(button.icon)
		button.icon = null
	await process_frame
	await process_frame
	RenderingServer.force_draw(false)
	var without_icons: Image = app.ui_viewport.get_texture().get_image()
	for i in icon_buttons.size():
		var button := icon_buttons[i]
		var changed := _changed_pixels(visible_icons, without_icons, button.get_global_rect())
		assert(changed > 20, button.name + " icon produces visible rendered pixels")
		button.icon = icon_textures[i]
		print("ICON_PIXELS ", button.name, " ", changed)
	for tab in app.ui_controller.precision.tabs:
		assert(tab.get_node("TabIcon").texture != null, "tab icon resource loaded")
		assert(tab.get_node("TabIcon").mouse_filter == Control.MOUSE_FILTER_IGNORE, "tab icon does not intercept input")

	app.session_lifecycle.stream_started()
	var welcome := WelcomeScreen.new(app)
	var welcome_root: Control = app.welcome_viewport.get_node("WelcomeRoot")
	welcome_root.theme = ProductTheme.create()
	var background := TextureRect.new()
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	background.texture = load("res://src/assets/early_twilight.png")
	background.expand_mode = TextureRect.EXPAND_FIT_WIDTH_PROPORTIONAL
	background.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_COVERED
	welcome_root.add_child(background)
	var screens := Control.new()
	screens.name = "Screens"
	screens.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	welcome_root.add_child(screens)
	welcome.build_welcome_screen(screens)
	welcome.build_server_screen(screens)
	welcome.build_ip_screen(screens)
	welcome.build_pin_screen(screens)
	screens.get_node("PINScreen/PINLabel").text = "1234"
	app.welcome_viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	for page in ["WelcomeScreen", "ServerScreen", "IPScreen", "PINScreen"]:
		for screen in screens.get_children():
			screen.visible = screen.name == page
		await process_frame
		await process_frame
		RenderingServer.force_draw(false)
		app.welcome_viewport.get_texture().get_image().save_png("../after-%s.png" % page)
		if page != "WelcomeScreen":
			var back: Button = null
			# Back is created dynamically, so inspect by its stable display text.
			for child in screens.get_node(page).get_children():
				if child is Button and child.text == "Back": back = child
			assert(back != null and back.icon != null, page + " Back icon resource loaded")
	app.native_xr_renderer.renderer.free()
	app.free()
	quit()
