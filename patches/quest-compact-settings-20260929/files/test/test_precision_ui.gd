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

func _init():
	_run.call_deferred()

func _hit(app, pixel: Vector2) -> Vector3:
	var local := Vector3((pixel.x / app._ui_viewport_size.x - 0.5) * app._ui_mesh_size.x,
		(0.5 - pixel.y / app._ui_viewport_size.y) * app._ui_mesh_size.y, 0)
	return app.ui_panel_3d.to_global(local)

class RecordedComputer extends RefCounted:
	var requests: Array = []
	var callback: Callable
	func request_pc_control(_host: int, body: Dictionary, cb: Callable):
		requests.append(body.duplicate(true))
		callback = cb
		cb.call(202, {"outcome": "pending"}, "")

func _check_bounds(node: Control, bounds: Rect2):
	if not node.is_visible_in_tree():
		return
	if node is Button or node is Label:
		assert(bounds.encloses(node.get_global_rect()), "Control escaped viewport/footer: " + str(node.get_path()) + " " + str(node.get_global_rect()))
	for child in node.get_children():
		if child is Control:
			_check_bounds(child, bounds)

func _run():
	# Instantiate the real complete 840x820 menu scene, without startup/network.
	var app = load("res://main.tscn").instantiate()
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
	app.settings.host.pc_profile_resolution = Vector2i(2560, 720)
	app.ui_controller.build_ui()
	await process_frame
	await process_frame

	var bar: Control = app.ui_viewport.find_child("CompGrabBar", true, false)
	var initial_rect := bar.get_global_rect()
	assert(initial_rect == Rect2(260, 756, 320, 56))
	assert(bar.get_node("MenuMoveLabel").text == "메뉴 이동")
	var panel: Control = app.ui_viewport.find_child("Panel", true, false)
	assert(panel.size == Vector2(840, 820))
	var precision = app.ui_controller.precision
	for id in [0, 1, 2, 3, 4, 6]:
		app.ui_controller.switch_tab(id)
		await process_frame
		_check_bounds(precision.pages[id], Rect2(0, 0, 840, 756))
		for child in precision.pages[id].get_children():
			if child is Button and child.has_meta("precision_subpage"):
				child.button_down.emit()
				await process_frame
				_check_bounds(precision.pages[id], Rect2(0, 0, 840, 756))
	assert(precision.tabs.size() == 4)
	app.ui_controller.switch_tab(0)
	for name in precision.view_panels:
		precision._show_view_panel(name)
		await process_frame
		_check_bounds(precision.pages[0], Rect2(0, 0, 840, 756))
	precision._show_view_panel("Overview")
	assert(ProductTheme.BG.r < 0.15 and ProductTheme.TEXT.r > 0.9, "Menu dark theme only")
	var expected_tabs := ["Display", "3D", "Quality", "Connect"]
	for i in expected_tabs.size():
		assert(precision.tabs[i].get_node("TabLabel").text == expected_tabs[i])
	assert(precision.pages[3].get_node_or_null("SubnavSurface") == null, "Depth has no unrelated display subtabs")
	for tab_id in [2, 4]:
		var page: Control = precision.pages[tab_id]
		var nav: Control = page.get_node("SubnavSurface")
		var heading: Control = page.get_node("SectionHeading")
		assert(nav.position.y + nav.size.y < heading.position.y, "Subnavigation and content heading have distinct vertical spacing")

	assert(precision.pages[6].get_node_or_null("SubnavSurface") == null, "PC connection settings have no redundant one-item subtab")
	assert(precision.pages[6].get_node("ConnectionSettingsTitle").text == "연결 설정")
	for button in precision.tabs:
		assert(button.size.y >= 60)
	assert(app._ui_sharpen_btn.get_signal_connection_list("button_down").size() > 0)
	assert(app.settings_controller._video_sampling_button.get_signal_connection_list("pressed").size() > 0)
	for i in 4:
		assert(app.ui_controller._presentation_buttons[i].get_signal_connection_list("button_down").size() > 0)
	app.ui_controller.switch_tab(3)
	for child in precision.pages[3].get_children():
		if child is Button and child.get_meta("precision_subpage", -1) == 0:
			child.button_down.emit()
	var depth_help: Label = precision.pages[3].find_child("DepthHelp", true, false)
	assert(depth_help.position.x == precision.depth_buttons[0].position.x and depth_help.position.y >= precision.depth_sliders[0].get_rect().end.y, "Depth hint belongs immediately below the Depth controls")
	assert(not app._ui_sbs_btn.is_visible_in_tree(), "PC SBS format is not an actionable button")
	assert(precision.pages[3].find_child("SbsFormatStatus", true, false) == null, "PC page has no redundant format status row")
	for id in [2, 6]:
		app.ui_controller.switch_tab(id)
		for child in precision.pages[id].find_children("*", "Button", true, false):
			assert(not child.is_visible_in_tree() or child.text != "버튼 설정", "PC profile exposes no hidden mapping toggle")
	assert(not app._ui_double_click_btn.is_visible_in_tree() and not app._ui_ctrl_mode_btn.is_visible_in_tree() and not app._ui_ctrl_type_btn.is_visible_in_tree() and not app._ui_btn_toggle_btn.is_visible_in_tree())
	assert(not app.controller_mapper.is_input_mapping_supported())
	assert(not app.controller_mapper.active and state.saves == 0, "Opening each settings page does not activate or save PC mapping")
	# Actual production size control round-trip preserves video aspect ratio.
	var original_size: Vector2 = app.primary_screen.mesh_size
	app.ui_controller._presentation_buttons[1].button_down.emit()
	assert(app.primary_screen.mesh_size.x > original_size.x)
	assert(is_equal_approx(app.primary_screen.mesh_size.aspect(), original_size.aspect()))
	app.ui_controller._presentation_buttons[0].button_down.emit()
	assert(app.primary_screen.mesh_size.is_equal_approx(original_size))
	# Use the real visible controls against a recording transport; keep frame ACK gating.
	var cm := RecordedComputer.new()
	app.stream_backend = StreamBackend.new(null)
	app.stream_backend.set_computer_manager(cm)
	var pc = app.pc_control
	pc._host = 7
	pc._on_status(200, {"version": 1, "session_id": "precision-fixture", "revision": 4, "last_seq": 9, "requested_mode": "3d", "effective_mode": "3d", "disparity": 25.2, "eye_width": 1920, "publisher_age_ms": 1.0, "applied_request": null, "rejected_request": null, "fallback_reason": null}, "", pc._generation)
	assert(precision.depth_labels[0].text == "1.31%")
	precision.depth_buttons[1].button_down.emit()
	assert(cm.requests.size() == 1 and is_equal_approx(cm.requests[0].disparity, 26.16))
	precision.depth_buttons[1].button_down.emit()
	assert(cm.requests.size() == 1)
	var ack: Dictionary = pc.status.duplicate(true)
	ack.disparity = 26.16
	ack.applied_request = cm.requests[0].request_id
	ack.revision = 5
	ack.last_seq = 10
	pc._on_status(200, ack, "", pc._generation)
	assert(precision.depth_labels[0].text == "1.36%")
	# A slider drag sends nothing until release, then exactly one bounded request.
	var slider = precision.depth_sliders[0]
	var event := InputEventMouseButton.new()
	event.button_index = MOUSE_BUTTON_LEFT
	event.position = slider.size * 0.5
	event.pressed = true
	slider.gui_input.emit(event)
	assert(cm.requests.size() == 1)
	event.pressed = false
	slider.gui_input.emit(event)
	assert(cm.requests.size() == 2 and is_equal_approx(cm.requests[1].disparity, 38.4))
	assert(cm.requests[1].mode == "3d" and not cm.requests[1].has("disparity_profile"))

	# A status poll can overlap a slider release; the same revision commits once after it completes.
	var ack2: Dictionary = pc.status.duplicate(true)
	ack2.disparity = cm.requests[1].disparity
	ack2.applied_request = cm.requests[1].request_id
	ack2.revision = 6
	ack2.last_seq = 11
	pc._on_status(200, ack2, "", pc._generation)
	pc._busy = true
	event.position.x = 100
	event.pressed = true
	slider.gui_input.emit(event)
	event.pressed = false
	slider.gui_input.emit(event)
	assert(cm.requests.size() == 2 and not precision._queued_depth.is_empty())
	pc._busy = false
	pc.refresh_ui()
	assert(cm.requests.size() == 3 and precision._queued_depth.is_empty())
	# Navigating to SBS/3D settings is presentation only, never a media-format request.
	var protocol_count: int = cm.requests.size()
	var before_profile: Vector2i = app.settings.host.pc_profile_resolution
	var before_sbs: int = app.settings.host.effective_sbs_mode()
	app._ui_sbs_btn.button_down.emit()
	assert(app.ui_controller._current_tab == 3 and cm.requests.size() == protocol_count)
	assert(app.settings.host.pc_profile_resolution == before_profile and app.settings.host.effective_sbs_mode() == before_sbs)
	# Disabled/offline controls cannot issue an operation.
	pc._status_at = 0
	pc.refresh_ui()
	assert(slider.disabled and precision.mode_sets[0][0].disabled)
	precision.mode_sets[0][0].button_down.emit()
	assert(cm.requests.size() == 3)
	cm.callback = Callable()
	pc.pending.clear()
	pc._busy = false
	print("Precision UI PASS: all subpages bounded, original controls bound, mode/depth/slider and frame ACK gate")
	app.ui_controller.switch_tab(0)
	await process_frame
	var pixel := initial_rect.get_center()
	assert(pointer._is_ui_grab_bar(pixel))
	assert(not pointer._is_ui_grab_bar(initial_rect.position - Vector2(1, 1)))
	assert(not pointer._is_ui_grab_bar(Vector2(NAN, 1)))
	assert(not pointer._is_ui_grab_bar(Vector2(420, 821)))
	var style: StyleBoxFlat = bar.get_theme_stylebox("panel")
	var idle_alpha: float = style.bg_color.a
	app.set_comp_grab_bar_color(app.ui_viewport, Color(1, 1, 1, .25))
	assert(bar.get_theme_stylebox("panel").bg_color.a > idle_alpha)
	assert(bar.get_node("MenuMoveLabel").is_visible_in_tree())
	assert(pointer._menu_pixel_from_hit(_hit(app, pixel)).is_equal_approx(pixel), "World pose and visible hit rectangle agree")
	# Actual production pointer routing starts only at a fresh press on the bar.
	var start_position: Vector3 = app.ui_panel_3d.global_position
	var video_transform: Transform3D = app.primary_screen.global_transform
	var video_size: Vector2 = app.primary_screen.mesh_size
	app.was_clicking = true
	pointer._route_menu_pointer(_hit(app, pixel), true, app.mouse_raycast)
	assert(app.grabbed_node == null, "Held gesture must not be picked up by the handle")
	app.was_clicking = false
	pointer.held = true
	pointer._route_menu_pointer(_hit(app, pixel), true, app.mouse_raycast)
	assert(app.grabbed_node == app.ui_panel_3d and app.was_clicking)
	assert(pointer.click_events.is_empty(), "Starting a panel drag emits no UI or PC click")
	app.mouse_raycast.position = Vector3(.05, .02, -.01)
	pointer.handle_grab()
	assert(app.ui_panel_3d.global_position.is_equal_approx(start_position + Vector3(.30, .12, -.12)))
	# Moving the ray over a normal button while dragging cannot activate it.
	app.ui_controller.switch_tab(1)
	await process_frame
	var exit_pixel: Vector2 = app._ui_exit_btn.get_global_rect().get_center()
	pointer._route_menu_pointer(_hit(app, exit_pixel), true, app.mouse_raycast)
	assert(pointer.click_events.is_empty() and app.ordinary_clicks == 0)
	pointer.held = false
	pointer.handle_grab()
	assert(app.grabbed_node == null and not app.was_clicking and state.saves == 1)
	assert(app._ui_has_saved_offset, "Release records the menu offset for reopen")
	assert(app.primary_screen.global_transform == video_transform and app.primary_screen.mesh_size == video_size)
	# Repeated drag works without first moving off the handle after release.
	pointer.held = true
	pointer._route_menu_pointer(_hit(app, pixel), true, app.mouse_raycast)
	assert(app.grabbed_node == app.ui_panel_3d)
	pointer.held = false
	pointer.handle_grab()
	assert(state.saves == 2)
	# Ordinary button input still reaches the real SubViewport/Button signal.
	pointer._route_menu_pointer(_hit(app, exit_pixel), true, app.mouse_raycast)
	await process_frame
	pointer._route_menu_pointer(_hit(app, exit_pixel), false, app.mouse_raycast)
	await process_frame
	assert(app.grabbed_node == null and app.ordinary_clicks == 1)
	assert(pointer.click_events.size() == 2)
	# Navigate through actual ray/Button input, with no mapping or settings side effect.
	var advanced_button: Button
	for child in precision.pages[1].get_children():
		if child is Button and child.text == "연결 설정":
			advanced_button = child
	assert(advanced_button != null)
	var advanced_pixel := advanced_button.get_global_rect().get_center()
	pointer._route_menu_pointer(_hit(app, advanced_pixel), true, app.mouse_raycast)
	await process_frame
	pointer._route_menu_pointer(_hit(app, advanced_pixel), false, app.mouse_raycast)
	await process_frame
	assert(app.ui_controller._current_tab == 6 and not app.controller_mapper.active and state.saves == 2)
	var back_button: Button
	for child in precision.pages[6].get_children():
		if child is Button and child.text == "Connect":
			back_button = child
	assert(back_button != null)
	var back_pixel := back_button.get_global_rect().get_center()
	pointer._route_menu_pointer(_hit(app, back_pixel), true, app.mouse_raycast)
	await process_frame
	pointer._route_menu_pointer(_hit(app, back_pixel), false, app.mouse_raycast)
	await process_frame
	assert(app.ui_controller._current_tab == 1 and not app.controller_mapper.active and state.saves == 2)
	# Hidden/queued handles are not targets. Closing cancels an active drag.
	pointer.held = true
	pointer._route_menu_pointer(_hit(app, pixel), true, app.mouse_raycast)
	assert(app.grabbed_node == app.ui_panel_3d)
	app.ui_visible = false
	var before_hide: Transform3D = app.ui_panel_3d.global_transform
	app.mouse_raycast.position += Vector3.ONE
	pointer.handle_grab()
	assert(app.grabbed_node == null and app.ui_panel_3d.global_transform == before_hide)
	app.ui_visible = true
	pointer._route_menu_pointer(_hit(app, pixel), true, app.mouse_raycast)
	assert(app.grabbed_node == null, "A held trigger after hide cannot replay")
	bar.hide()
	assert(not pointer._is_ui_grab_bar(pixel))
	bar.show()
	bar.queue_free()
	assert(not pointer._is_ui_grab_bar(pixel))
	app.free()
	print("Menu move handle PASS: fixed bottom footer/no content overlap, production ray routing/drag/release, button input, repeated gesture and hidden lifetime")
	quit()
