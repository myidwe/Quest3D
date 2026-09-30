class_name PrecisionUI
extends RefCounted

var _ui_ref: WeakRef
var ui: UIController:
	get: return _ui_ref.get_ref() if _ui_ref else null
var main: Node3D
var pages: Dictionary = {}
var tabs: Array[Button] = []
var mode_sets: Array = []
var option_buttons: Array[Button] = []
var depth_sliders: Array[PrecisionDepthSlider] = []
var depth_labels: Array[Label] = []
var depth_buttons: Array[Button] = []
var pc_notes: Array[Label] = []
var _status: Label
var _last_pc_visual: String = ""
var _queued_depth: Dictionary = {}
var view_panels: Dictionary = {}
var view_controls: Dictionary = {}
var geometry_buttons: Array[Button] = []
var _repeat_button: Button
var _repeat_action: Callable
var _repeat_at := 0

func _init(controller: UIController):
	_ui_ref = weakref(controller)
	main = ui.main

func _rect(control: Control, parent: Control, rect: Rect2):
	if control.get_parent():
		control.reparent(parent)
	else:
		parent.add_child(control)
	control.set_anchors_and_offsets_preset(Control.PRESET_TOP_LEFT)
	control.custom_minimum_size = Vector2.ZERO
	control.position = rect.position
	control.size = rect.size
	control.visible = true

func _label(parent: Control, text: String, rect: Rect2, font_size: int = 24, strong: bool = false) -> Label:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_font_override("font", ProductTheme.MEDIUM if strong else ProductTheme.FONT)
	label.add_theme_color_override("font_color", ProductTheme.TEXT)
	label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(label, parent, rect)
	return label

func _button(parent: Control, text: String, rect: Rect2, icon_name: String = "") -> Button:
	var button := Button.new()
	button.text = text
	button.focus_mode = Control.FOCUS_NONE
	button.add_theme_font_size_override("font_size", 24)
	if not icon_name.is_empty():
		button.icon = ProductTheme.icon(icon_name)
		button.expand_icon = true
		button.add_theme_constant_override("icon_max_width", 28)
		button.add_theme_constant_override("h_separation", 20)
	_rect(button, parent, rect)
	return button

func _line(parent: Control, y: float):
	var line := ColorRect.new()
	line.color = ProductTheme.BORDER
	line.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(line, parent, Rect2(0, y, 1104, 1))

func _style_button(button: Button, selected: bool = false, border: bool = false):
	var key := Vector2i(int(selected), int(border))
	if button.get_meta("precision_style", Vector2i(-1, -1)) == key:
		return
	button.set_meta("precision_style", key)
	var normal := ProductTheme.surface(ProductTheme.PALE if selected else ProductTheme.RAISED, 12, 8)
	if border or selected:
		normal.border_color = ProductTheme.BORDER
		normal.set_border_width_all(1)
	button.add_theme_stylebox_override("normal", normal)
	button.add_theme_stylebox_override("hover", ProductTheme.surface(ProductTheme.PALE, 12, 8))
	button.add_theme_stylebox_override("pressed", ProductTheme.surface(Color("#49322C"), 12, 8))
	button.add_theme_color_override("font_color", ProductTheme.ACCENT if selected else ProductTheme.TEXT)
	button.add_theme_color_override("font_hover_color", ProductTheme.ACCENT)
	button.add_theme_color_override("font_pressed_color", ProductTheme.ACCENT)
	button.set_meta("dual_hover_norm", normal)

func install(root: Control):
	var panel: Control = root.get_node("Panel")
	var legacy: Control = panel.get_node("MenuLayout/VBox")
	legacy.visible = false
	var brand_old := root.get_node_or_null("Brand")
	if brand_old:
		brand_old.visible = false
	var layout: Control = panel.get_node("MenuLayout")
	var header := Control.new()
	header.name = "PrecisionHeader"
	header.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(header, layout, Rect2(32, 14, 1136, 72))
	var logo := TextureRect.new()
	logo.texture = preload("res://src/assets/precision/quest3d-mark.png")
	logo.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	logo.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	logo.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(logo, header, Rect2(0, 2, 62, 62))
	_label(header, "Quest3D", Rect2(68, 2, 360, 66), 36, true)
	_rect(main._ui_close_btn, header, Rect2(1072, 2, 60, 60))
	main._ui_close_btn.text = ""
	main._ui_close_btn.icon = ProductTheme.icon("x")
	main._ui_close_btn.expand_icon = true
	main._ui_close_btn.add_theme_constant_override("icon_max_width", 30)
	main._ui_close_btn.tooltip_text = "메뉴 닫기"
	_style_button(main._ui_close_btn)
	var tab_bar := Control.new()
	tab_bar.name = "PrecisionTabs"
	tab_bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(tab_bar, layout, Rect2(28, 92, 1144, 64))
	var specs := [[ui._tab_btn_display, "Display", "monitor", 0], [ui._tab_btn_ai3d, "3D", "box", 3], [ui._tab_btn_picture, "Quality", "sliders-horizontal", 4], [ui._tab_btn_stream, "Connect", "wifi", 1]]
	for i in specs.size():
		var button: Button = specs[i][0]
		_rect(button, tab_bar, Rect2(i * 286, 0, 272, 64))
		button.text = ""
		var tab_icon := TextureRect.new()
		tab_icon.name = "TabIcon"
		tab_icon.mouse_filter = Control.MOUSE_FILTER_IGNORE
		tab_icon.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
		_rect(tab_icon, button, Rect2(51, 17, 30, 30))
		var tab_label := _label(button, specs[i][1], Rect2(102, 0, 156, 64), 28, true)
		tab_label.name = "TabLabel"
		button.set_meta("precision_icon", specs[i][2])
		button.set_meta("precision_tab", specs[i][3])
		button.expand_icon = true
		button.add_theme_constant_override("icon_max_width", 30)
		button.add_theme_constant_override("h_separation", 22)
		button.add_theme_font_size_override("font_size", 28)
		button.add_theme_font_override("font", ProductTheme.MEDIUM)
		tabs.append(button)
	var header_rule := ColorRect.new()
	header_rule.color = ProductTheme.BORDER
	header_rule.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(header_rule, layout, Rect2(48, 167, 1104, 1))
	for id in [0, 1, 2, 3, 4, 6]:
		var page := Control.new()
		page.name = "PrecisionPage%d" % id
		page.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_rect(page, layout, Rect2(48, 180, 1104, 340))
		pages[id] = page
	ui._tab_display = pages[0]
	ui._tab_stream = pages[1]
	ui._tab_control = pages[2]
	ui._tab_ai3d = pages[3]
	ui._tab_picture = pages[4]
	ui._tab_advanced = pages[6]
	_build_overview(pages[0])
	_build_spatial(pages[3])
	_build_quality(pages[4])
	_build_connection(pages[1])
	_build_controller(pages[2])
	_build_advanced(pages[6])
	_status = main._ui_status_label
	_rect(_status, pages[1], Rect2(0, 292, 1104, 34))
	_status.add_theme_color_override("font_color", ProductTheme.MUTED)
	_status.add_theme_font_size_override("font_size", 20)
	_status.max_lines_visible = 1
	var handle: PanelContainer = layout.get_node("MenuMoveRow/CompGrabBar")
	handle.add_theme_stylebox_override("panel", ProductTheme.surface(Color.TRANSPARENT, 18, 0))
	var text: Label = handle.get_node("MenuMoveLabel")
	text.text = "메뉴 이동"
	text.add_theme_color_override("font_color", ProductTheme.MUTED)
	text.add_theme_font_size_override("font_size", 20)
	text.vertical_alignment = VERTICAL_ALIGNMENT_BOTTOM
	var grip := Panel.new()
	grip.name = "PrecisionGrip"
	grip.mouse_filter = Control.MOUSE_FILTER_IGNORE
	grip.add_theme_stylebox_override("panel", ProductTheme.surface(Color("#ADB3BA"), 4, 0))
	var grip_layer := Control.new()
	grip_layer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	handle.add_child(grip_layer)
	_rect(grip, grip_layer, Rect2(254, 3, 92, 7))
	var timer := Timer.new()
	timer.wait_time = 0.25
	timer.autostart = true
	timer.timeout.connect(refresh_labels)
	layout.add_child(timer)
	main.settings_controller.refresh_color_balance_controls()
	main.settings_controller.refresh_video_sampling_controls()
	ui.set_disconnect_visible(main.is_streaming)
	ui.switch_tab(0)
	refresh_pc()
	refresh_labels()

func _mode_depth(parent: Control, y: float):
	_label(parent, "Mode", Rect2(0, y, 150, 46), 24, true)
	var a := _button(parent, "2D", Rect2(138, y + 20, 134, 60))
	var b := _button(parent, "3D", Rect2(272, y + 20, 134, 60))
	a.button_down.connect(func(): main.pc_control.set_mode("2d"))
	b.button_down.connect(func(): main.pc_control.set_mode("3d"))
	mode_sets.append([a, b])
	var divider := ColorRect.new()
	divider.color = ProductTheme.BORDER
	divider.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(divider, parent, Rect2(442, y, 1, 86))
	_label(parent, "Depth", Rect2(480, y, 200, 38), 24, true)
	var minus := _button(parent, "", Rect2(480, y + 40, 60, 60), "minus")
	var plus := _button(parent, "", Rect2(1044, y + 40, 60, 60), "plus")
	minus.tooltip_text = "입체감 0.05% 감소"
	plus.tooltip_text = "입체감 0.05% 증가"
	minus.button_down.connect(func(): main.pc_control.change_depth(-PcControl.DEPTH_STEP_FRACTION))
	plus.button_down.connect(func(): main.pc_control.change_depth(PcControl.DEPTH_STEP_FRACTION))
	depth_buttons.append(minus)
	depth_buttons.append(plus)
	var slider := PrecisionDepthSlider.new()
	slider.name = "DepthSlider"
	_rect(slider, parent, Rect2(556, y + 40, 300, 60))
	slider.depth_selected.connect(_queue_depth)
	depth_sliders.append(slider)
	var depth := _label(parent, "—", Rect2(870, y + 40, 160, 60), 30, true)
	depth.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	depth_labels.append(depth)

func _build_overview(page: Control):
	var overview := _view_panel(page, "Overview")
	_label(overview, "Mode", Rect2(0, 0, 100, 56), 24, true)
	var two := _button(overview, "2D", Rect2(112, 0, 116, 56))
	var three := _button(overview, "3D", Rect2(236, 0, 116, 56))
	two.button_down.connect(func(): main.pc_control.set_mode("2d"))
	three.button_down.connect(func(): main.pc_control.set_mode("3d"))
	mode_sets.append([two, three])
	_view_action(overview, "Center", Rect2(644, 0, 218, 56), "Center", func(): main.screen_manager.center_view())
	view_controls.lock = _view_action(overview, "Lock · Off", Rect2(880, 0, 224, 56), "Lock", func(): main.screen_manager.set_view_locked(not main.screen_manager.view_locked), false)
	_line(overview, 76)
	_label(overview, "Size", Rect2(0, 88, 200, 36), 26, true)
	view_controls.size_value = _label(overview, "", Rect2(700, 88, 404, 36), 24)
	view_controls.size_value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_view_action(overview, "−", Rect2(0, 132, 66, 60), "SizeMinus", func(): main.screen_manager.scale_primary_screen(1.0 / 1.05), true, true)
	view_controls.size_slider = _view_slider(overview, Rect2(82, 132, 940, 60), "SizeSlider", 0.6, 12, 0.05, func(v): main.screen_manager.set_view_width(v))
	_view_action(overview, "+", Rect2(1038, 132, 66, 60), "SizePlus", func(): main.screen_manager.scale_primary_screen(1.05), true, true)
	_label(overview, "가상 화면 크기 · 영상 해상도 유지", Rect2(0, 198, 1104, 30), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	for i in 4:
		var names := ["Position", "Views", "Environment", "Framing"]
		var action := _view_action(overview, names[i], Rect2(i * 280, 246, 264, 58), names[i] + "Open", func(): _show_view_panel(names[i]), false)
		action.set_meta("view_navigation", names[i])
	view_controls.note = _label(overview, "", Rect2(0, 309, 1104, 30), 20)
	view_controls.note.add_theme_color_override("font_color", ProductTheme.MUTED)
	_build_view_position(_view_panel(page, "Position", "위치와 가상 거리"))
	_build_view_presets(_view_panel(page, "Views", "보기 저장"))
	_build_view_environment(_view_panel(page, "Environment", "화면 형태와 주변 환경"))
	_build_view_framing(_view_panel(page, "Framing", "검은 여백 자르기"))
	_show_view_panel("Overview")
	var repeat := Timer.new()
	repeat.wait_time = 0.1
	repeat.autostart = true
	repeat.timeout.connect(_repeat_view_action)
	page.add_child(repeat)
	page.tree_exiting.connect(_stop_view_repeat)

func _view_panel(parent: Control, key: String, title: String = "") -> Control:
	var panel := Control.new()
	panel.name = "View" + key
	panel.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(panel, parent, Rect2(0, 0, 1104, 340))
	view_panels[key] = panel
	if not title.is_empty():
		var back := _view_action(panel, "Display", Rect2(0, 0, 172, 48), key + "Back", func(): _show_view_panel("Overview"), false)
		back.icon = ProductTheme.icon("arrow-left")
		back.expand_icon = true
		back.add_theme_constant_override("icon_max_width", 24)
		back.add_theme_constant_override("h_separation", 10)
		_label(panel, title, Rect2(202, 0, 750, 48), 26, true)
	return panel

func _show_view_panel(key: String):
	_stop_view_repeat()
	for name in view_panels:
		view_panels[name].visible = name == key
	_refresh_view()
	ui.refresh_ui_buttons()

func _view_action(parent: Control, text: String, rect: Rect2, key: String, action: Callable, geometry: bool = true, repeat: bool = false) -> Button:
	var button := _button(parent, text, rect)
	button.name = key
	_style_button(button, false, true)
	if geometry:
		geometry_buttons.append(button)
	button.button_down.connect(func():
		if button.disabled: return
		action.call()
		_refresh_view()
		if repeat:
			_repeat_button = button
			_repeat_action = action
			_repeat_at = Time.get_ticks_msec() + 500
	)
	button.button_up.connect(func():
		if _repeat_button == button: _stop_view_repeat()
	)
	button.mouse_exited.connect(func():
		if _repeat_button == button: _stop_view_repeat()
	)
	return button

func _stop_view_repeat():
	_repeat_button = null
	_repeat_action = Callable()
	_repeat_at = 0

func _repeat_view_action():
	if not is_instance_valid(_repeat_button):
		_stop_view_repeat()
		return
	if not main.ui_visible or not _repeat_button.is_visible_in_tree() or _repeat_button.disabled or not _repeat_button.button_pressed:
		_stop_view_repeat()
		return
	if Time.get_ticks_msec() >= _repeat_at:
		_repeat_action.call()
		_repeat_at = Time.get_ticks_msec() + 150
		_refresh_view()

func _view_slider(parent: Control, rect: Rect2, key: String, low: float, high: float, step: float, action: Callable) -> PrecisionDepthSlider:
	var slider := PrecisionDepthSlider.new()
	slider.name = key
	slider.minimum = low
	slider.maximum = high
	slider.step = step
	_rect(slider, parent, rect)
	slider.depth_selected.connect(func(v):
		if not slider.disabled and slider.is_visible_in_tree(): action.call(v)
		_refresh_view()
	)
	return slider

func _build_view_position(page: Control):
	_label(page, "Distance", Rect2(0, 66, 226, 58), 24, true)
	_view_action(page, "−", Rect2(230, 66, 66, 58), "DistanceMinus", func(): main.screen_manager.move_primary_screen(-0.1), true, true)
	view_controls.distance_slider = _view_slider(page, Rect2(304, 66, 496, 58), "DistanceSlider", 0.5, 10, 0.1, func(v): main.screen_manager.set_view_distance(v))
	view_controls.distance_value = _label(page, "", Rect2(808, 66, 208, 58), 26, true)
	view_controls.distance_value.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_view_action(page, "+", Rect2(1038, 66, 66, 58), "DistancePlus", func(): main.screen_manager.move_primary_screen(0.1), true, true)
	view_controls.keep = _view_action(page, "", Rect2(0, 137, 520, 50), "KeepSize", func(): main.screen_manager.set_keep_view_size(not main.screen_manager.keep_view_size), false)
	_label(page, "거리 변경 시 가상 크기도 함께 조절", Rect2(552, 137, 552, 50), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(page, "Move · 10 cm", Rect2(0, 202, 500, 28), 22, true)
	_label(page, "Tilt · 2°", Rect2(568, 202, 536, 28), 22, true)
	var moves := [Vector2(-0.1, 0), Vector2(0.1, 0), Vector2(0, 0.1), Vector2(0, -0.1)]
	var labels := ["←", "→", "↑", "↓"]
	for i in 4:
		_view_action(page, labels[i], Rect2(i * 134, 241, 118, 58), "Move%d" % i, func(): main.screen_manager.shift_view(moves[i]), true, true)
	_view_action(page, "−", Rect2(568, 241, 106, 58), "TiltMinus", func(): main.screen_manager.tilt_view(-2), true, true)
	_view_action(page, "+", Rect2(690, 241, 106, 58), "TiltPlus", func(): main.screen_manager.tilt_view(2), true, true)
	_view_action(page, "Level · 수평", Rect2(812, 241, 292, 58), "Level", func(): main.screen_manager.level_view())
	view_controls.position_note = _label(page, "", Rect2(0, 310, 1104, 28), 20)

func _build_view_presets(page: Control):
	var names := ["Standard", "Cinema", "Reclined", "My view"]
	for i in 4:
		var button := _view_action(page, names[i], Rect2((i % 2) * 568, 74 + floori(i / 2.0) * 76, 536, 60), "Preset%d" % i, func(): main.screen_manager.apply_view_preset(names[i]))
		view_controls["preset_%d" % i] = button
	_view_action(page, "Save view", Rect2(0, 238, 264, 58), "SaveView", func(): main.screen_manager.save_view(), false)
	view_controls.saved_note = _label(page, "", Rect2(288, 238, 816, 58), 22)
	_label(page, "Reclined · 현재 시선에 배치   /   Depth·색감 유지", Rect2(0, 308, 1104, 30), 20).add_theme_color_override("font_color", ProductTheme.MUTED)

func _build_view_environment(page: Control):
	_label(page, "Curve", Rect2(0, 66, 170, 58), 24, true)
	for i in 3:
		var labels := ["Flat", "Gentle", "Curved"]
		view_controls["curve_%d" % i] = _view_action(page, labels[i], Rect2(190 + i * 304, 66, 288, 58), "Curve%d" % i, func(): main.screen_manager.set_view_curve(i))
	var options := Control.new()
	options.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(options, page, Rect2(0, 134, 1104, 206))
	var definitions := [[main._ui_bezel_btn, "Bezel"], [main._ui_pt_btn, "Passthrough"], [main._ui_bg_btn, "Background"], [main._ui_ambient_btn, "Ambient"], [main._ui_ambient_color_btn, "조명 색"]]
	for i in definitions.size(): _option(options, definitions[i][0], definitions[i][1], i)

func _build_view_framing(page: Control):
	_label(page, "좌우 눈 동일 적용 · 영상 안 자막도 잘림", Rect2(0, 62, 1104, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	for i in 2:
		var key := "crop_top" if i == 0 else "crop_bottom"
		_label(page, "Top" if i == 0 else "Bottom", Rect2(0, 112 + i * 76, 180, 60), 24, true)
		view_controls[key] = _view_slider(page, Rect2(188, 112 + i * 76, 700, 60), key, 0, 25, 0.5, func(v): main.screen_manager.set_view_crop(v / 100.0 if i == 0 else main.screen_manager.crop_top, v / 100.0 if i == 1 else main.screen_manager.crop_bottom))
		view_controls[key + "_value"] = _label(page, "", Rect2(924, 112 + i * 76, 180, 60), 26, true)
	_view_action(page, "Fit · 원본 복원", Rect2(0, 274, 284, 58), "RestoreFraming", func(): main.screen_manager.set_view_crop(0, 0))
	view_controls.framing_note = _label(page, "", Rect2(308, 274, 796, 58), 20)

func _refresh_view():
	var sm = main.screen_manager
	if not sm or view_controls.is_empty(): return
	var screen: VRScreen = main.primary_screen
	var ready := screen != null and main.xr_camera != null
	for button in geometry_buttons: button.disabled = not ready or sm.view_locked
	if view_controls.has("size_slider"):
		var bounds: Vector2 = sm.view_width_limits()
		view_controls.size_slider.minimum = bounds.x
		view_controls.size_slider.maximum = bounds.y
		view_controls.size_slider.disabled = not ready or sm.view_locked
		if ready:
			view_controls.size_slider.set_percent(screen.mesh_size.x)
			view_controls.size_value.text = "너비 %.2f m · 거리 %.2f m" % [screen.mesh_size.x, sm.view_distance()]
		view_controls.lock.text = "Lock · On" if sm.view_locked else "Lock · Off"
		_style_button(view_controls.lock, sm.view_locked, true)
		view_controls.note.text = "위치 잠금 · 해제 후 화면 조절" if sm.view_locked else (sm.view_message if not sm.view_message.is_empty() else sm.view_name + " · ± 5% · 길게 눌러 연속 조절")
	if view_controls.has("distance_slider"):
		view_controls.distance_slider.disabled = not ready or sm.view_locked
		view_controls.distance_slider.set_percent(sm.view_distance())
		view_controls.distance_value.text = "%.2f m" % sm.view_distance()
		view_controls.keep.text = "보이는 크기 고정 · " + ("On" if sm.keep_view_size else "Off")
		_style_button(view_controls.keep, sm.keep_view_size, true)
		view_controls.position_note.text = "위치 잠금 · Display에서 해제" if sm.view_locked else sm.view_message
	if view_controls.has("saved_note"):
		view_controls.saved_note.text = sm.view_message if not sm.view_message.is_empty() else "화면 배치만 저장 · 현재 PC별 보관"
		view_controls.preset_3.disabled = not ready or sm.view_locked or sm.saved_view.is_empty()
	for i in 3:
		if view_controls.has("curve_%d" % i): _style_button(view_controls["curve_%d" % i], ready and screen.curvature == i, true)
	for key in ["crop_top", "crop_bottom"]:
		if view_controls.has(key):
			view_controls[key].disabled = sm.view_locked or not sm.framing_available()
			view_controls[key].set_percent(sm.get(key) * 100.0)
			view_controls[key + "_value"].text = "%.1f%%" % (sm.get(key) * 100.0)
	if view_controls.has("framing_note"):
		view_controls.framing_note.text = "자르기 중 테두리 숨김" if sm.crop_top + sm.crop_bottom > 0 else ("원본 화면비 유지" if sm.framing_available() else "PC 영상 연결 후 조절")
	for key in ["menu_size", "pointer_size", "pointer_contrast"]:
		if not view_controls.has(key): continue
		view_controls[key].text = ("%.0f%%" % (sm.menu_scale * 100.0)) if key == "menu_size" else (("%.0f%%" % (sm.pointer_scale * 100.0)) if key == "pointer_size" else ("High" if sm.pointer_contrast else "Normal"))
	for key in ["size_slider", "distance_slider", "crop_top", "crop_bottom"]:
		if view_controls.has(key) and view_controls[key].disabled:
			view_controls[key].dragging = false

func _style_subtab(button: Button, selected: bool):
	var style := ProductTheme.surface(ProductTheme.RAISED if selected else Color.TRANSPARENT, 9, 6)
	button.add_theme_stylebox_override("normal", style)
	button.add_theme_stylebox_override("hover", ProductTheme.surface(ProductTheme.RAISED, 9, 6))
	button.add_theme_stylebox_override("pressed", ProductTheme.surface(ProductTheme.PALE, 9, 6))
	button.add_theme_stylebox_override("disabled", style)
	button.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	button.add_theme_color_override("font_color", ProductTheme.TEXT if selected else ProductTheme.MUTED)
	button.add_theme_color_override("font_hover_color", ProductTheme.TEXT)
	button.set_meta("dual_hover_norm", style)

func _subpages(page: Control, labels: Array, titles: Array) -> Array:
	var bodies: Array = []
	var buttons: Array[Button] = []
	var bar := Panel.new()
	bar.name = "SubnavSurface"
	bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	bar.add_theme_stylebox_override("panel", ProductTheme.surface(ProductTheme.PANEL, 12, 0))
	_rect(bar, page, Rect2(0, 0, 1104, 54))
	var heading := _label(page, titles[0], Rect2(0, 68, 1104, 30), 24, true)
	heading.name = "SectionHeading"
	for i in labels.size():
		var body := Control.new()
		body.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_rect(body, page, Rect2(0, 112, 1104, 224))
		body.visible = i == 0
		bodies.append(body)
		var button := _button(page, labels[i], Rect2(6 + i * 202, 5, 196, 44))
		button.add_theme_font_size_override("font_size", 22)
		button.set_meta("precision_subpage", i)
		buttons.append(button)
		button.button_down.connect(func():
			heading.text = titles[i]
			for j in bodies.size():
				bodies[j].visible = j == i
				_style_subtab(buttons[j], j == i)
			ui.refresh_ui_buttons()
		)
		_style_subtab(button, i == 0)
	return bodies

func _option(parent: Control, button: Button, caption: String, index: int):
	var x := float(index % 2) * 568
	var y := floorf(float(index) / 2.0) * 74
	var label := _label(parent, caption, Rect2(x, y, 172, 58), 22)
	button.set_meta("precision_caption", label)
	button.set_meta("precision_caption_fixed", caption)
	var pieces := button.text.split("\n")
	if pieces.size() >= 2:
		button.text = ProductTheme.text(pieces[1])
	_rect(button, parent, Rect2(x + 176, y, 360, 58))
	button.add_theme_font_size_override("font_size", 22)
	button.add_theme_color_override("font_color", ProductTheme.TEXT)
	button.add_theme_color_override("font_hover_color", ProductTheme.ACCENT)
	button.add_theme_color_override("font_pressed_color", ProductTheme.ACCENT)
	button.clip_text = true
	_style_button(button, false, true)
	option_buttons.append(button)

func _is_pc_profile() -> bool:
	return main.PC_SBS_BUILD or main.settings.host.pc_profile_resolution != Vector2i.ZERO

func _build_spatial(page: Control):
	_label(page, "Depth · 입체감", Rect2(0, 0, 1104, 42), 26, true)
	var body := Control.new()
	body.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(body, page, Rect2(0, 64, 1104, 264))
	_mode_depth(body, 0)
	if not _is_pc_profile():
		_option(body, main._ui_sbs_btn, "SBS", 4)
	var note := _label(body, "윤곽이 겹치면 Depth 낮추기", Rect2(480, 108, 624, 32), 20)
	note.name = "DepthHelp"
	note.add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(body, "윤곽 안정화 · PC Display에서 조절", Rect2(0, 202, 1104, 42), 22).add_theme_color_override("font_color", ProductTheme.MUTED)

func _build_quality(page: Control):
	var parts := _subpages(page, ["Sharpness", "Color", "Tint", "Stream"], ["선명도와 샘플링", "밝기와 명암", "색온도와 색조", "영상 전송"])
	_option(parts[0], main._ui_sharpen_btn, "Sharpening", 0)
	_option(parts[0], main.settings_controller._video_sampling_button, "Sampling", 1)
	_rect(ui._stream_resolution_note, parts[0], Rect2(0, 74, 1104, 40))
	ui._stream_resolution_note.add_theme_color_override("font_color", ProductTheme.TEXT)
	ui._stream_resolution_note.add_theme_font_size_override("font_size", 24)
	_label(parts[0], "Quality: 영상 보정 · Runtime Quality: Quest 화면 합성", Rect2(0, 128, 1104, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(parts[0], "Current: 현재 선명도 유지 · Supersample: 확대 샘플링", Rect2(0, 174, 1104, 32), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	var color_controls := [[main._ui_brightness_btn, "Brightness"], [main._ui_contrast_btn, "Contrast"], [main._ui_gamma_btn, "Gamma"]]
	for i in color_controls.size():
		_option(parts[1], color_controls[i][0], color_controls[i][1], i)
	# Reuse actual color buttons with their original pressed bindings.
	var root := main.get_node("%UIRoot")
	for i in 5:
		var names := ["ColorCooler", "ColorWarmer", "ColorGreener", "ColorMagenta", "ColorReset"]
		var captions := ["색온도 −", "색온도 +", "초록빛", "자홍빛", "색조 복원"]
		var button: Button = root.find_child(names[i], true, false)
		_option(parts[2], button, captions[i], i)
	for i in 4:
		var buttons := [main._ui_res_btn, main._ui_fps_btn, main._ui_bitrate_btn, main._ui_codec_btn]
		var captions := ["Eye Resolution", "FPS", "Bitrate", "Codec"]
		_option(parts[3], buttons[i], captions[i], i)
	_label(parts[3], "전송 설정 변경 시 영상 재연결 가능", Rect2(0, 180, 1104, 38), 20).add_theme_color_override("font_color", ProductTheme.MUTED)

func _build_connection(page: Control):
	_rect(main._ui_host_label, page, Rect2(0, 0, 1104, 48))
	main._ui_host_label.add_theme_color_override("font_color", ProductTheme.TEXT)
	main._ui_host_label.add_theme_font_size_override("font_size", 28)
	var center := _button(page, "Center", Rect2())
	center.button_down.connect(func(): main.screen_manager.center_view())
	geometry_buttons.append(center)
	var actions := [[center, "화면 정렬"], [main._ui_stats_btn, "성능 표시"], [main._ui_disconnect_btn, "연결 해제"], [main._ui_exit_btn, "앱 종료"]]
	for i in actions.size():
		var button: Button = actions[i][0]
		_rect(button, page, Rect2(i * 278, 66, 258, 62))
		button.text = actions[i][1]
		button.add_theme_font_size_override("font_size", 24)
		_style_button(button, false, true)
	_option(page, main._ui_quick_start_btn, "빠른 연결", 4)
	_option(page, main._ui_host_cursor_btn, "PC 마우스", 5)
	var controller := _button(page, "Menu / Pointer", Rect2(0, 244, 240, 48))
	controller.button_down.connect(ui.switch_tab.bind(2))
	var advanced := _button(page, "연결 설정", Rect2(258, 244, 240, 48))
	advanced.button_down.connect(ui.switch_tab.bind(6))

func _build_controller(page: Control):
	var back := _button(page, "Connect", Rect2(0, 0, 160, 48), "arrow-left")
	back.button_down.connect(ui.switch_tab.bind(1))
	var parts := _subpages(page, ["Pointer", "Menu", "Input"], ["포인터", "설정창 크기와 위치", "컨트롤러"])
	back.position.x = 944
	page.move_child(back, -1)
	_option(parts[0], main._ui_cursor_btn, "모양", 0)
	_option(parts[0], main._ui_steady_btn, "떨림 보정", 1)
	view_controls.pointer_size = _view_action(parts[0], "", Rect2(), "PointerSize", func():
		var sm = main.screen_manager
		sm.set_pointer_style(1.0 if sm.pointer_scale >= 1.5 else (1.5 if sm.pointer_scale >= 1.2 else 1.2), sm.pointer_contrast), false)
	_option(parts[0], view_controls.pointer_size, "크기", 2)
	view_controls.pointer_contrast = _view_action(parts[0], "", Rect2(), "PointerContrast", func():
		var sm = main.screen_manager
		sm.set_pointer_style(sm.pointer_scale, not sm.pointer_contrast), false)
	_option(parts[0], view_controls.pointer_contrast, "대비", 3)
	_label(parts[0], "설정창 포인터 · PC 마우스와 별도", Rect2(0, 178, 1104, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	view_controls.menu_size = _view_action(parts[1], "", Rect2(), "MenuSize", func():
		var sm = main.screen_manager
		sm.set_menu_scale(0.85 if sm.menu_scale >= 1.2 else (1.2 if sm.menu_scale >= 1.0 else 1.0)), false)
	_option(parts[1], view_controls.menu_size, "크기", 0)
	var recenter := _view_action(parts[1], "Center", Rect2(), "MenuCenter", func(): main.screen_manager.center_menu(), false)
	_option(parts[1], recenter, "정면 배치", 1)
	_label(parts[1], "위치 이동 · 설정창 하단 손잡이", Rect2(0, 110, 1104, 36), 22).add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(parts[1], "PC 영상 크기와 별도", Rect2(0, 162, 1104, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_option(parts[2], main._ui_hand_tracking_btn, "손 추적", 0)
	_option(parts[2], main._ui_primary_btn, "주 사용 손", 1)
	if not _is_pc_profile():
		var more := _button(parts[2], "버튼 설정", Rect2(0, 176, 240, 58))
		more.button_down.connect(func():
			main.controller_mapper.check_toggle_ui()
		)

func _build_advanced(page: Control):
	var back := _button(page, "Connect", Rect2(0, 0, 160, 48), "arrow-left")
	back.button_down.connect(ui.switch_tab.bind(1))
	if _is_pc_profile():
		var title := _label(page, "연결 설정", Rect2(0, 76, 1104, 40), 26, true)
		title.name = "ConnectionSettingsTitle"
		var body := Control.new()
		body.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_rect(body, page, Rect2(0, 134, 1104, 196))
		_option(body, main._ui_reconnect_btn, "자동 재연결", 0)
		_option(body, main._ui_idle_btn, "미사용 연결 해제", 1)
		return
	var parts := _subpages(page, ["Reconnect", "Input"], ["자동 연결", "컨트롤러 입력"])
	# The ordinary-host input settings retain the original controls and behavior.
	back.position.x = 938
	page.move_child(back, -1)
	_option(parts[0], main._ui_reconnect_btn, "자동 재연결", 0)
	_option(parts[0], main._ui_idle_btn, "미사용 연결 해제", 1)
	var definitions := [[main._ui_double_click_btn, "더블 클릭"], [main._ui_ctrl_mode_btn, "버튼 설정"], [main._ui_ctrl_type_btn, "기기 모드"], [main._ui_btn_toggle_btn, "전환 방식"]]
	for i in definitions.size():
		_option(parts[1], definitions[i][0], definitions[i][1], i)

func refresh_tabs(tab: int):
	_stop_view_repeat()
	for button in tabs:
		var selected: bool = int(button.get_meta("precision_tab")) == tab or (int(button.get_meta("precision_tab")) == 1 and tab in [2, 6])
		var style := ProductTheme.surface(ProductTheme.PALE if selected else Color.TRANSPARENT, 15, 10)
		button.add_theme_stylebox_override("normal", style)
		button.add_theme_stylebox_override("hover", ProductTheme.surface(ProductTheme.PALE, 15, 10))
		button.add_theme_color_override("font_color", ProductTheme.ACCENT if selected else ProductTheme.TEXT)
		button.add_theme_color_override("font_hover_color", ProductTheme.ACCENT)
		button.add_theme_color_override("font_pressed_color", ProductTheme.ACCENT)
		button.icon = null
		button.get_node("TabIcon").texture = ProductTheme.icon(button.get_meta("precision_icon"), selected)
		button.get_node("TabLabel").add_theme_color_override("font_color", ProductTheme.ACCENT if selected else ProductTheme.TEXT)
		button.set_meta("dual_hover_norm", style)
	refresh_labels()
	ui._refresh_stream_resolution()

func refresh_pc():
	_try_depth_commit()
	var pc = main.pc_control
	var ready: bool = pc.available() and not pc._busy
	var effect := String(pc.status.get("effective_mode", ""))
	var visual := str([ready, effect, pc.status.get("disparity"), pc.status.get("eye_width"), pc.pending.is_empty(), pc._can_retry])
	if visual == _last_pc_visual:
		return
	_last_pc_visual = visual
	for modes in mode_sets:
		for i in 2:
			modes[i].disabled = not ready or (not pc.pending.is_empty() and i == 1 and not pc._can_retry)
			_style_button(modes[i], effect == ("2d" if i == 0 else "3d"))
	var percent := 100.0 * float(pc.status.get("disparity", 0.0)) / maxf(1.0, float(pc.status.get("eye_width", 1920)))
	for label in depth_labels:
		label.text = "%.2f%%" % percent if not pc.status.is_empty() else "—"
	for slider in depth_sliders:
		slider.disabled = not pc.available() or not pc.pending.is_empty() or not _queued_depth.is_empty()
		slider.set_percent(percent)
	for button in depth_buttons:
		button.disabled = not ready or not pc.pending.is_empty()
	# Update cached ray-hover styles after a confirmed PC state change.
	if ui.precision == self:
		ui.refresh_ui_buttons()

func refresh_labels():
	for button in option_buttons:
		if not is_instance_valid(button):
			continue
		var lines := button.text.split("\n")
		if lines.size() >= 2:
			button.text = ProductTheme.text(lines[1])
		var caption: Label = button.get_meta("precision_caption")
		caption.text = button.get_meta("precision_caption_fixed")
		button.add_theme_color_override("font_color", ProductTheme.TEXT)
		button.size.y = 58
	_refresh_view()

func _queue_depth(percent: float):
	var pc = main.pc_control
	if not main.ui_visible or not pc.available() or not pc.pending.is_empty():
		return
	_queued_depth = {"percent": percent, "session": pc.status.get("session_id"), "revision": pc.status.get("revision"), "at": Time.get_ticks_msec()}
	_try_depth_commit()

func _try_depth_commit():
	if _queued_depth.is_empty():
		return
	var pc = main.pc_control
	if not main.ui_visible or Time.get_ticks_msec() - int(_queued_depth.at) > 2000 or pc.status.get("session_id") != _queued_depth.session or pc.status.get("revision") != _queued_depth.revision:
		_queued_depth.clear()
		_last_pc_visual = ""
		return
	if not pc.available() or pc._busy or not pc.pending.is_empty():
		return
	var percent: float = _queued_depth.percent
	_queued_depth.clear()
	pc._send(String(pc.status.get("requested_mode", "2d")), float(pc.status.get("eye_width", 1920)) * percent / 100.0)
