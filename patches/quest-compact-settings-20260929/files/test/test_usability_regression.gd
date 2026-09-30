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

class OrdinarySettings extends RefCounted:
	var cycles := 0
	func cycle_sbs_mode():
		cycles += 1

class OrdinaryHost extends Node3D:
	const PC_SBS_BUILD := false
	var settings := AppSettings.new()
	var auto_detect_enabled := true
	var settings_controller := OrdinarySettings.new()

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

	var precision = app.ui_controller.precision
	app.ui_controller.switch_tab(3)
	await process_frame
	var hint: Label
	for node in precision.pages[3].find_children("*", "Label", true, false):
		if "윤곽이" in node.text:
			hint = node
	var info := {"depth_hint_x": hint.position.x if hint else -1, "sbs_clickable_visible": app._ui_sbs_btn.is_visible_in_tree()}
	app.ui_controller.switch_tab(2)
	await process_frame
	var action: Button
	for node in precision.pages[2].find_children("*", "Button", true, false):
		if node.text == "버튼 설정" and node.is_visible_in_tree():
			action = node
	info["mapping_button_visible"] = action != null
	info["mapping_before"] = app.controller_mapper.active
	info["saves_before"] = state.saves
	if action:
		action.button_down.emit()
	info["mapping_after_click"] = app.controller_mapper.active
	info["saves_after_click"] = state.saves
	print("USABILITY_PROBE ", JSON.stringify(info))
	if "--expect-fixed" in OS.get_cmdline_user_args():
		assert(info.depth_hint_x == precision.depth_buttons[0].position.x)
		app.ui_controller.switch_tab(3)
		await process_frame
		var hint_rect := hint.get_global_rect()
		var depth_row: Rect2 = precision.depth_sliders[0].get_global_rect()
		var minus_row: Rect2 = precision.depth_buttons[0].get_global_rect()
		assert(hint_rect.position.x == minus_row.position.x, "Help aligns to its Depth control column")
		assert(hint_rect.position.y >= maxf(depth_row.end.y, minus_row.end.y), "Help sits below the real Depth controls")
		_check_bounds(precision.pages[3], Rect2(0, 0, 840, 756))

		assert(not info.sbs_clickable_visible)
		assert(not info.mapping_button_visible)
		assert(not info.mapping_after_click and info.saves_after_click == info.saves_before)
		var ordinary := OrdinaryHost.new()
		var ordinary_ui := UIController.new(ordinary)
		ordinary_ui.on_sbs_toggled()
		assert(not ordinary.auto_detect_enabled and ordinary.settings_controller.cycles == 1, "Ordinary-host SBS keeps its original cycle action")
		ordinary.free()
		print("Usability regression PASS: Depth-aligned help, no redundant PC format, no mapping action or state change")
	app.free()
	quit()
