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
var _stream_info: Label
var _tint_buttons: Dictionary = {}
var _tint_values: Array[Label] = []
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
	_rect(line, parent, Rect2(0, y, 776, 1))

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
	if brand_old: brand_old.visible = false
	var shell := ProductTheme.surface(ProductTheme.BG, 28, 0)
	shell.border_color = ProductTheme.BORDER.darkened(0.3)
	shell.set_border_width_all(1)
	panel.add_theme_stylebox_override("panel", shell)
	var layout: Control = panel.get_node("MenuLayout")
	var header := Control.new()
	header.name = "PrecisionHeader"
	header.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(header, layout, Rect2(32, 18, 776, 56))
	var logo := TextureRect.new()
	logo.texture = preload("res://src/assets/precision/quest3d-mark.png")
	logo.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	logo.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	logo.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(logo, header, Rect2(0, 6, 44, 44))
	_label(header, "Sterevi", Rect2(58, 0, 260, 56), 28, true)
	_rect(main._ui_close_btn, header, Rect2(724, 2, 52, 52))
	main._ui_close_btn.text = ""
	main._ui_close_btn.icon = ProductTheme.icon("x")
	main._ui_close_btn.expand_icon = true
	main._ui_close_btn.add_theme_constant_override("icon_max_width", 26)
	main._ui_close_btn.tooltip_text = "메뉴 닫기"
	_style_button(main._ui_close_btn)
	var tab_bar := Control.new()
	tab_bar.name = "PrecisionTabs"
	tab_bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(tab_bar, layout, Rect2(24, 92, 792, 64))
	var specs := [[ui._tab_btn_display, "Display", "monitor", 0], [ui._tab_btn_ai3d, "3D", "box", 3], [ui._tab_btn_picture, "Quality", "sliders-horizontal", 4], [ui._tab_btn_stream, "Connect", "wifi", 1]]
	for i in specs.size():
		var button: Button = specs[i][0]
		_rect(button, tab_bar, Rect2(i * 198, 0, 192, 64))
		button.text = ""
		var tab_icon := TextureRect.new()
		tab_icon.name = "TabIcon"
		tab_icon.mouse_filter = Control.MOUSE_FILTER_IGNORE
		tab_icon.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
		var label_width := ProductTheme.MEDIUM.get_string_size(specs[i][1], HORIZONTAL_ALIGNMENT_LEFT, -1, 24).x
		var start := (192.0 - 26.0 - 12.0 - label_width) * 0.5
		_rect(tab_icon, button, Rect2(start, 19, 26, 26))
		var tab_label := _label(button, specs[i][1], Rect2(start + 38, 0, label_width + 2, 64), 24, true)
		tab_label.name = "TabLabel"
		button.set_meta("precision_icon", specs[i][2])
		button.set_meta("precision_tab", specs[i][3])
		tabs.append(button)
	var header_rule := ColorRect.new()
	header_rule.color = ProductTheme.BORDER
	header_rule.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(header_rule, layout, Rect2(32, 171, 776, 1))
	for id in [0, 1, 2, 3, 4, 6]:
		var page := Control.new()
		page.name = "PrecisionPage%d" % id
		page.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_rect(page, layout, Rect2(32, 188, 776, 674))
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
	_rect(_status, pages[1], Rect2(0, 498, 776, 48))
	_status.add_theme_color_override("font_color", ProductTheme.MUTED)
	_status.add_theme_font_size_override("font_size", 20)
	_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_status.max_lines_visible = 2
	var row: Control = layout.get_node("MenuMoveRow")
	row.offset_top = -64
	row.offset_bottom = -8
	var handle: PanelContainer = row.get_node("CompGrabBar")
	handle.custom_minimum_size = Vector2(320, 52)
	handle.add_theme_stylebox_override("panel", ProductTheme.surface(Color.TRANSPARENT, 18, 0))
	var label: Label = handle.get_node("MenuMoveLabel")
	label.text = "메뉴 이동"
	label.add_theme_color_override("font_color", ProductTheme.MUTED)
	label.add_theme_font_size_override("font_size", 18)
	label.vertical_alignment = VERTICAL_ALIGNMENT_BOTTOM
	var grip_layer := Control.new()
	grip_layer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	handle.add_child(grip_layer)
	var grip := Panel.new()
	grip.name = "PrecisionGrip"
	grip.mouse_filter = Control.MOUSE_FILTER_IGNORE
	grip.add_theme_stylebox_override("panel", ProductTheme.surface(Color("#ADB3BA"), 4, 0))
	_rect(grip, grip_layer, Rect2(124, 8, 72, 6))
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
	_label(parent, "Mode", Rect2(0, y, 240, 60), 24, true)
	var a := _button(parent, "2D", Rect2(488, y, 138, 60))
	var b := _button(parent, "3D", Rect2(638, y, 138, 60))
	a.button_down.connect(func(): main.pc_control.set_mode("2d"))
	b.button_down.connect(func(): main.pc_control.set_mode("3d"))
	mode_sets.append([a, b])
	_line(parent, y + 88)
	_label(parent, "Depth", Rect2(0, y + 116, 300, 52), 28, true)
	var depth := _label(parent, "—", Rect2(536, y + 108, 240, 60), 38, true)
	depth.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	depth_labels.append(depth)
	var minus := _button(parent, "", Rect2(0, y + 188, 60, 60), "minus")
	var plus := _button(parent, "", Rect2(716, y + 188, 60, 60), "plus")
	minus.tooltip_text = "입체감 0.05% 감소"
	plus.tooltip_text = "입체감 0.05% 증가"
	minus.button_down.connect(func(): main.pc_control.change_depth(-PcControl.DEPTH_STEP_FRACTION))
	plus.button_down.connect(func(): main.pc_control.change_depth(PcControl.DEPTH_STEP_FRACTION))
	depth_buttons.append(minus)
	depth_buttons.append(plus)
	var slider := PrecisionDepthSlider.new()
	slider.name = "DepthSlider"
	_rect(slider, parent, Rect2(76, y + 188, 624, 60))
	slider.depth_selected.connect(_queue_depth)
	depth_sliders.append(slider)

func _build_overview(page: Control):
	var overview := _view_panel(page, "Overview")
	_label(overview, "Mode", Rect2(0, 0, 240, 60), 24, true)
	var two := _button(overview, "2D", Rect2(488, 0, 138, 60))
	var three := _button(overview, "3D", Rect2(638, 0, 138, 60))
	two.button_down.connect(func(): main.pc_control.set_mode("2d"))
	three.button_down.connect(func(): main.pc_control.set_mode("3d"))
	mode_sets.append([two, three])
	_line(overview, 88)
	_label(overview, "Size", Rect2(0, 108, 160, 42), 28, true)
	view_controls.size_value = _label(overview, "", Rect2(180, 108, 596, 42), 24)
	view_controls.size_value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_view_action(overview, "−", Rect2(0, 164, 60, 60), "SizeMinus", func(): main.screen_manager.scale_primary_screen(1.0 / 1.05), true, true)
	view_controls.size_slider = _view_slider(overview, Rect2(76, 164, 624, 60), "SizeSlider", 0.6, 12, 0.05, func(v): main.screen_manager.set_view_width(v))
	_view_action(overview, "+", Rect2(716, 164, 60, 60), "SizePlus", func(): main.screen_manager.scale_primary_screen(1.05), true, true)
	_label(overview, "화면 너비 · 영상 해상도 유지", Rect2(0, 234, 776, 28), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_view_action(overview, "Center", Rect2(0, 308, 380, 60), "Center", func(): main.screen_manager.center_view())
	view_controls.lock = _view_action(overview, "Lock · Off", Rect2(396, 308, 380, 60), "Lock", func(): main.screen_manager.set_view_locked(not main.screen_manager.view_locked), false)
	var names := ["Position", "Views", "Environment", "Framing"]
	var hints := ["거리 · 수평 · 회전", "프리셋 · 내 화면 저장", "곡률 · 배경 · 조명", "상하 여백 자르기"]
	for i in 4:
		var tile := _view_action(overview, "", Rect2((i % 2) * 396, 412 + floori(i / 2.0) * 100, 380, 86), names[i] + "Open", func(): _show_view_panel(names[i]), false)
		_tile_labels(tile, names[i], hints[i])
		tile.set_meta("view_navigation", names[i])
	view_controls.note = _label(overview, "", Rect2(0, 636, 776, 28), 19)
	view_controls.note.add_theme_color_override("font_color", ProductTheme.MUTED)
	_build_view_position(_view_panel(page, "Position", "거리와 위치"))
	_build_view_presets(_view_panel(page, "Views", "화면 배치"))
	_build_view_environment(_view_panel(page, "Environment", "형태와 주변 환경"))
	_build_view_framing(_view_panel(page, "Framing", "상하 여백"))
	_show_view_panel("Overview")
	var repeat := Timer.new()
	repeat.wait_time = 0.1
	repeat.autostart = true
	repeat.timeout.connect(_repeat_view_action)
	page.add_child(repeat)
	page.tree_exiting.connect(_stop_view_repeat)

func _tile_labels(button: Button, title: String, hint: String):
	_label(button, title, Rect2(18, 9, button.size.x - 36, 30), 23, true)
	_label(button, hint, Rect2(18, 42, button.size.x - 36, 26), 19).add_theme_color_override("font_color", ProductTheme.MUTED)

func _view_panel(parent: Control, key: String, title: String = "") -> Control:
	var panel := Control.new()
	panel.name = "View" + key
	panel.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(panel, parent, Rect2(0, 0, 776, 674))
	view_panels[key] = panel
	if not title.is_empty():
		var back := _view_action(panel, "", Rect2(0, 0, 56, 56), key + "Back", func(): _show_view_panel("Overview"), false)
		back.icon = ProductTheme.icon("arrow-left")
		back.expand_icon = true
		back.add_theme_constant_override("icon_max_width", 24)
		back.tooltip_text = "Display로 돌아가기"
		_label(panel, key, Rect2(76, 0, 350, 32), 28, true)
		_label(panel, title, Rect2(76, 34, 500, 26), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	return panel

func _show_view_panel(key: String):
	_stop_view_repeat()
	for name in view_panels:
		view_panels[name].visible = name == key
	_refresh_view()
	ui.refresh_ui_buttons()

func _view_action(parent: Control, text: String, rect: Rect2, key: String, action: Callable, geometry: bool = true, repeat: bool = false) -> Button:
	var icon_name := "minus" if text == "−" else ("plus" if text == "+" else "")
	var button := _button(parent, "" if not icon_name.is_empty() else text, rect, icon_name)
	if not icon_name.is_empty(): button.tooltip_text = "줄이기" if icon_name == "minus" else "늘리기"
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
	_label(page, "Alignment", Rect2(0, 88, 300, 56), 24, true)
	view_controls.align_level = _view_action(page, "Level", Rect2(396, 88, 184, 56), "AlignLevel", func(): main.screen_manager.set_level_lock(true))
	view_controls.align_free = _view_action(page, "Free", Rect2(592, 88, 184, 56), "AlignFree", func(): main.screen_manager.set_level_lock(false))
	view_controls.align_hint = _label(page, "", Rect2(0, 154, 776, 28), 20)
	view_controls.align_hint.add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(page, "Distance", Rect2(0, 214, 350, 36), 24, true)
	view_controls.distance_value = _label(page, "", Rect2(488, 210, 288, 44), 30, true)
	view_controls.distance_value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	view_controls.distance_minus = _view_action(page, "−", Rect2(0, 266, 60, 60), "DistanceMinus", func(): main.screen_manager.move_primary_screen(-0.25), true, true)
	view_controls.distance_minus.tooltip_text = "25 cm 가까이"
	view_controls.distance_slider = _view_slider(page, Rect2(76, 266, 624, 60), "DistanceSlider", 0.5, 10, 0.1, func(v): main.screen_manager.set_view_distance(v))
	view_controls.distance_plus = _view_action(page, "+", Rect2(716, 266, 60, 60), "DistancePlus", func(): main.screen_manager.move_primary_screen(0.25), true, true)
	view_controls.distance_plus.tooltip_text = "25 cm 멀리"
	view_controls.keep = _view_action(page, "", Rect2(0, 342, 380, 58), "KeepSize", func(): main.screen_manager.set_keep_view_size(not main.screen_manager.keep_view_size), false)
	view_controls.keep_hint = _label(page, "", Rect2(404, 342, 372, 58), 20)
	view_controls.keep_hint.add_theme_color_override("font_color", ProductTheme.MUTED)
	_line(page, 426)
	_label(page, "Move · 10 cm", Rect2(0, 448, 376, 32), 22, true)
	_label(page, "Rotation · 2°", Rect2(416, 448, 360, 32), 22, true)
	var moves := [Vector2(-0.1, 0), Vector2(0.1, 0), Vector2(0, 0.1), Vector2(0, -0.1)]
	var icons := ["arrow-left", "arrow-right", "arrow-up", "arrow-down"]
	var hints := ["왼쪽으로 10 cm", "오른쪽으로 10 cm", "위로 10 cm", "아래로 10 cm"]
	for i in 4:
		var move := _view_action(page, "", Rect2(i * 94, 496, 80, 60), "Move%d" % i, func(): main.screen_manager.shift_view(moves[i]), true, true)
		move.icon = ProductTheme.icon(icons[i])
		move.expand_icon = true
		move.add_theme_constant_override("icon_max_width", 28)
		move.tooltip_text = hints[i]
	_label(page, "Tilt", Rect2(416, 496, 92, 56), 22)
	_label(page, "Roll", Rect2(416, 572, 92, 56), 22)
	view_controls.tilt_minus = _view_action(page, "−", Rect2(528, 496, 116, 56), "TiltMinus", func(): main.screen_manager.tilt_view(-2), true, true)
	view_controls.tilt_plus = _view_action(page, "+", Rect2(660, 496, 116, 56), "TiltPlus", func(): main.screen_manager.tilt_view(2), true, true)
	view_controls.roll_minus = _view_action(page, "−", Rect2(528, 572, 116, 56), "RollMinus", func(): main.screen_manager.roll_view(-2), true, true)
	view_controls.roll_plus = _view_action(page, "+", Rect2(660, 572, 116, 56), "RollPlus", func(): main.screen_manager.roll_view(2), true, true)
	_view_action(page, "수평 맞추기", Rect2(0, 572, 362, 56), "Level", func(): main.screen_manager.level_view())
	view_controls.position_note = _label(page, "", Rect2(0, 642, 776, 28), 19)

func _build_view_presets(page: Control):
	var names := ["Standard", "Cinema", "Reclined", "My view"]
	var hints := ["평면 · 너비 2.4 m", "완만한 곡면 · 너비 4 m", "시선 각도 · Free", "저장한 화면 불러오기"]
	for i in 4:
		var button := _view_action(page, "", Rect2((i % 2) * 396, 100 + floori(i / 2.0) * 100, 380, 86), "Preset%d" % i, func(): main.screen_manager.apply_view_preset(names[i]))
		_tile_labels(button, names[i], hints[i])
		view_controls["preset_%d" % i] = button
	_line(page, 314)
	_view_action(page, "Save view", Rect2(0, 340, 248, 60), "SaveView", func(): main.screen_manager.save_view(), false)
	_label(page, "현재 배치를 My view에 저장", Rect2(272, 340, 504, 60), 22)
	view_controls.saved_note = _label(page, "", Rect2(0, 426, 776, 36), 20)
	_label(page, "화면 배치만 적용 · Depth와 색감 유지", Rect2(0, 508, 776, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)

func _build_view_environment(page: Control):
	_label(page, "Curve", Rect2(0, 84, 776, 36), 24, true)
	for i in 3:
		var labels := ["Flat", "Gentle", "Curved"]
		view_controls["curve_%d" % i] = _view_action(page, labels[i], Rect2(i * 264, 132, 248, 58), "Curve%d" % i, func(): main.screen_manager.set_view_curve(i))
	var options := Control.new()
	options.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(options, page, Rect2(0, 220, 776, 330))
	var definitions := [[main._ui_bezel_btn, "Bezel"], [main._ui_pt_btn, "Passthrough"], [main._ui_bg_btn, "Background"], [main._ui_ambient_btn, "Ambient"], [main._ui_ambient_color_btn, "조명 색"]]
	for i in definitions.size(): _option(options, definitions[i][0], definitions[i][1], i, 66)

func _build_view_framing(page: Control):
	_label(page, "좌우 눈 동일 적용 · 영상 안 자막도 잘림", Rect2(0, 88, 776, 32), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	for i in 2:
		var key := "crop_top" if i == 0 else "crop_bottom"
		var y := 152 + i * 128
		_label(page, "Top" if i == 0 else "Bottom", Rect2(0, y, 350, 36), 24, true)
		view_controls[key + "_value"] = _label(page, "", Rect2(536, y, 240, 36), 28, true)
		view_controls[key + "_value"].horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
		view_controls[key] = _view_slider(page, Rect2(0, y + 44, 776, 60), key, 0, 25, 0.5, func(v): main.screen_manager.set_view_crop(v / 100.0 if i == 0 else main.screen_manager.crop_top, v / 100.0 if i == 1 else main.screen_manager.crop_bottom))
	_view_action(page, "Fit · 원본 복원", Rect2(0, 424, 280, 60), "RestoreFraming", func(): main.screen_manager.set_view_crop(0, 0))
	view_controls.framing_note = _label(page, "", Rect2(308, 424, 468, 60), 20)

func _refresh_view():
	var sm = main.screen_manager
	if not sm or view_controls.is_empty(): return
	var screen: VRScreen = main.primary_screen
	var ready := screen != null and main.xr_camera != null
	for button in geometry_buttons: button.disabled = not ready or sm.view_locked
	for key in ["tilt_minus", "tilt_plus", "roll_minus", "roll_plus"]:
		if view_controls.has(key): view_controls[key].disabled = not ready or sm.view_locked or sm.level_lock
	for prefix in ["align_", "menu_align_"]:
		if view_controls.has(prefix + "level"):
			_style_button(view_controls[prefix + "level"], sm.level_lock, true)
			_style_button(view_controls[prefix + "free"], not sm.level_lock, true)
	if view_controls.has("align_hint"):
		view_controls.align_hint.text = "영상과 메뉴 수평 유지" if sm.level_lock else "시선에 맞춰 배치 · Tilt 기울기 / Roll 좌우 회전"
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
		view_controls.note.text = "위치 잠금 · 해제 후 화면 조절" if sm.view_locked else ("Level · 수평 유지" if sm.level_lock else "Free · 자유 회전") + "   /   " + sm.view_name
	if view_controls.has("distance_slider"):
		var bounds: Vector2 = sm.view_distance_limits()
		var distance_ready := ready and bounds.y > bounds.x
		if distance_ready:
			view_controls.distance_slider.minimum = bounds.x
			view_controls.distance_slider.maximum = bounds.y
		view_controls.distance_slider.disabled = not distance_ready or sm.view_locked
		view_controls.distance_minus.disabled = not distance_ready or sm.view_locked or sm.view_distance() <= bounds.x + 0.0001
		view_controls.distance_plus.disabled = not distance_ready or sm.view_locked or sm.view_distance() >= bounds.y - 0.0001
		view_controls.distance_slider.set_percent(sm.view_distance())
		view_controls.distance_value.text = "%.2f m" % sm.view_distance()
		view_controls.keep.text = "보이는 크기 유지 · " + ("On" if sm.keep_view_size else "Off")
		view_controls.keep_hint.text = "거리와 너비 함께 조절" if sm.keep_view_size else "너비 유지 · 멀수록 작게"
		_style_button(view_controls.keep, sm.keep_view_size, true)
		view_controls.position_note.text = "위치 잠금 · Display에서 해제" if sm.view_locked else sm.view_message
		if not sm.view_locked and sm.view_message.is_empty():
			view_controls.position_note.text = "거리 %.1f–%.1f m · 버튼 25 cm" % [bounds.x, bounds.y] if distance_ready else "PC 연결 후 거리 조절"
		view_controls.position_note.add_theme_color_override("font_color", ProductTheme.MUTED)
	if view_controls.has("saved_note"):
		view_controls.saved_note.text = sm.view_message if not sm.view_message.is_empty() else "현재 PC의 화면 배치 저장"
		view_controls.preset_3.disabled = not ready or sm.view_locked or sm.saved_view.is_empty()
	for i in 3:
		if view_controls.has("curve_%d" % i): _style_button(view_controls["curve_%d" % i], ready and screen.curvature == i, true)
	for key in ["crop_top", "crop_bottom"]:
		if view_controls.has(key):
			view_controls[key].disabled = sm.view_locked or not sm.framing_available()
			view_controls[key].set_percent(sm.get(key) * 100.0)
			view_controls[key + "_value"].text = "%.1f%%" % (sm.get(key) * 100.0)
	if view_controls.has("framing_note"):
		view_controls.framing_note.text = "자른 화면의 테두리 숨김" if sm.crop_top + sm.crop_bottom > 0 else ("원본 화면비 유지" if sm.framing_available() else "PC 연결 후 조절 가능")
	for key in ["menu_size", "pointer_size", "pointer_contrast"]:
		if not view_controls.has(key): continue
		view_controls[key].text = ("%.0f%%" % (sm.menu_scale * 100.0)) if key == "menu_size" else (("%.0f%%" % (sm.pointer_scale * 100.0)) if key == "pointer_size" else ("High" if sm.pointer_contrast else "Normal"))
	for key in ["preset_0", "preset_1", "preset_2", "preset_3"]:
		if view_controls.has(key):
			for child in view_controls[key].get_children():
				if child is Label: child.modulate.a = 0.45 if view_controls[key].disabled else 1.0
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
	var width := 600.0 if page == pages[2] or page == pages[6] else 776.0
	var bar := Panel.new()
	bar.name = "SubnavSurface"
	bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	bar.add_theme_stylebox_override("panel", ProductTheme.surface(ProductTheme.PANEL, 12, 0))
	_rect(bar, page, Rect2(0, 0, width, 60))
	var heading := _label(page, titles[0], Rect2(0, 84, 776, 32), 24, true)
	heading.name = "SectionHeading"
	for i in labels.size():
		var body := Control.new()
		body.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_rect(body, page, Rect2(0, 140, 776, 534))
		body.visible = i == 0
		bodies.append(body)
		var slot := (width - 8.0) / labels.size()
		var button := _button(page, labels[i], Rect2(4 + i * slot, 4, slot - 4, 52))
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

func _option(parent: Control, button: Button, caption: String, index: int, stride: float = 76):
	var y := index * stride
	var label := _label(parent, caption, Rect2(0, y, 302, 58), 22)
	button.set_meta("precision_caption", label)
	button.set_meta("precision_caption_fixed", caption)
	var pieces := button.text.split("\n")
	if pieces.size() >= 2: button.text = ProductTheme.text(pieces[1])
	_rect(button, parent, Rect2(326, y, 450, 58))
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
	_mode_depth(page, 0)
	var note := _label(page, "윤곽이 겹치면 Depth 낮추기", Rect2(0, 270, 776, 32), 20)
	note.name = "DepthHelp"
	note.add_theme_color_override("font_color", ProductTheme.MUTED)
	_line(page, 344)
	_label(page, "윤곽 안정화", Rect2(0, 370, 776, 40), 24, true)
	_label(page, "PC → Display에서 조절", Rect2(0, 416, 776, 32), 22).add_theme_color_override("font_color", ProductTheme.MUTED)
	if not _is_pc_profile():
		_option(page, main._ui_sbs_btn, "SBS", 6)

func _build_quality(page: Control):
	var parts := _subpages(page, ["Sharpness", "Color", "Tint", "Stream"], ["선명도와 샘플링", "밝기와 명암", "색온도와 색조", "영상 전송"])
	_option(parts[0], main._ui_sharpen_btn, "Sharpening", 0)
	_option(parts[0], main.settings_controller._video_sampling_button, "Sampling", 1)
	_rect(ui._stream_resolution_note, parts[0], Rect2(0, 168, 776, 40))
	ui._stream_resolution_note.add_theme_color_override("font_color", ProductTheme.TEXT)
	ui._stream_resolution_note.add_theme_font_size_override("font_size", 24)
	ui._stream_resolution_note.horizontal_alignment = HORIZONTAL_ALIGNMENT_LEFT
	_label(parts[0], "Quality · 영상 보정", Rect2(0, 244, 776, 30), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(parts[0], "Runtime Quality · Quest 화면 합성", Rect2(0, 282, 776, 30), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(parts[0], "Current · 기본   /   Supersample · 확대 후 축소", Rect2(0, 340, 776, 30), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	var color_controls := [[main._ui_brightness_btn, "Brightness"], [main._ui_contrast_btn, "Contrast"], [main._ui_gamma_btn, "Gamma"]]
	for i in color_controls.size(): _option(parts[1], color_controls[i][0], color_controls[i][1], i)
	_build_tint(parts[2])
	for i in 4:
		var buttons := [main._ui_res_btn, main._ui_fps_btn, main._ui_bitrate_btn, main._ui_codec_btn]
		var captions := ["Eye Resolution", "FPS", "Bitrate", "Codec"]
		_option(parts[3], buttons[i], captions[i], i)
	_stream_info = _label(parts[3], "", Rect2(0, 310, 776, 40), 24)
	_stream_info.name = "StreamEyeInfo"
	_label(parts[3], "설정 변경 시 잠시 재연결", Rect2(0, 366, 776, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)

func _build_tint(body: Control):
	var root := main.get_node("%UIRoot")
	for i in 2:
		var y := i * 148
		_label(body, "Temperature" if i == 0 else "Tint", Rect2(0, y, 536, 36), 24, true)
		var value := _label(body, "+0", Rect2(536, y, 240, 36), 28, true)
		value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
		_tint_values.append(value)
		var names := ["ColorCooler", "ColorWarmer"] if i == 0 else ["ColorGreener", "ColorMagenta"]
		var captions := ["차갑게", "따뜻하게"] if i == 0 else ["초록빛", "자홍빛"]
		for j in 2:
			var button: Button = root.find_child(names[j], true, false)
			_rect(button, body, Rect2(j * 396, y + 50, 380, 60))
			_style_button(button, false, true)
			button.pressed.connect(refresh_labels)
			_tint_buttons[button] = captions[j]
	var reset: Button = root.find_child("ColorReset", true, false)
	_rect(reset, body, Rect2(0, 318, 280, 60))
	_style_button(reset, false, true)
	reset.pressed.connect(refresh_labels)
	_tint_buttons[reset] = "Reset · 기본 색감"
	_label(body, "밝기와 명암 유지", Rect2(308, 318, 468, 60), 20).add_theme_color_override("font_color", ProductTheme.MUTED)

func _build_connection(page: Control):
	_rect(main._ui_host_label, page, Rect2(0, 0, 776, 52))
	main._ui_host_label.add_theme_color_override("font_color", ProductTheme.TEXT)
	main._ui_host_label.add_theme_font_size_override("font_size", 28)
	main._ui_host_label.clip_text = true
	var center := _button(page, "Center", Rect2())
	center.button_down.connect(func(): main.screen_manager.center_view())
	geometry_buttons.append(center)
	var actions := [[center, "화면 정렬"], [main._ui_stats_btn, "성능 표시"], [main._ui_disconnect_btn, "연결 해제"], [main._ui_exit_btn, "앱 종료"]]
	for i in actions.size():
		var button: Button = actions[i][0]
		_rect(button, page, Rect2((i % 2) * 396, 80 if i < 2 else 418, 380, 60))
		button.text = actions[i][1]
		button.add_theme_font_size_override("font_size", 24)
		_style_button(button, false, true)
	var options := Control.new()
	options.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_rect(options, page, Rect2(0, 174, 776, 134))
	_option(options, main._ui_quick_start_btn, "빠른 연결", 0)
	_option(options, main._ui_host_cursor_btn, "PC 마우스", 1)
	var controller := _button(page, "Menu / Pointer", Rect2(0, 332, 380, 60))
	controller.button_down.connect(ui.switch_tab.bind(2))
	_style_button(controller, false, true)
	var advanced := _button(page, "연결 설정", Rect2(396, 332, 380, 60))
	advanced.button_down.connect(ui.switch_tab.bind(6))
	_style_button(advanced, false, true)

func _build_controller(page: Control):
	var back := _button(page, "Connect", Rect2(0, 0, 160, 56), "arrow-left")
	back.button_down.connect(ui.switch_tab.bind(1))
	var parts := _subpages(page, ["Pointer", "Menu", "Input"], ["포인터", "설정창 크기와 위치", "컨트롤러"])
	back.position.x = 616
	back.add_theme_constant_override("h_separation", 8)
	back.add_theme_font_size_override("font_size", 22)
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
	_label(parts[0], "설정창 포인터 · PC 마우스와 별도", Rect2(0, 328, 776, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	view_controls.menu_size = _view_action(parts[1], "", Rect2(), "MenuSize", func():
		var sm = main.screen_manager
		sm.set_menu_scale(0.85 if sm.menu_scale >= 1.2 else (1.2 if sm.menu_scale >= 1.0 else 1.0)), false)
	_option(parts[1], view_controls.menu_size, "크기", 0)
	var recenter := _view_action(parts[1], "Center", Rect2(), "MenuCenter", func(): main.screen_manager.center_menu(), false)
	_option(parts[1], recenter, "정면 배치", 1)
	_label(parts[1], "하단 손잡이로 메뉴 이동", Rect2(0, 192, 776, 36), 22).add_theme_color_override("font_color", ProductTheme.MUTED)
	_label(parts[1], "PC 영상 크기와 별도", Rect2(0, 244, 776, 36), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_line(parts[1], 302)
	_label(parts[1], "Alignment", Rect2(0, 328, 300, 56), 24, true)
	view_controls.menu_align_level = _view_action(parts[1], "Level", Rect2(396, 328, 184, 56), "MenuAlignLevel", func(): main.screen_manager.set_level_lock(true))
	view_controls.menu_align_free = _view_action(parts[1], "Free", Rect2(592, 328, 184, 56), "MenuAlignFree", func(): main.screen_manager.set_level_lock(false))
	_label(parts[1], "Level · 수평 유지   /   Free · 시선 각도\n영상과 메뉴에 함께 적용", Rect2(0, 404, 776, 66), 20).add_theme_color_override("font_color", ProductTheme.MUTED)
	_option(parts[2], main._ui_hand_tracking_btn, "손 추적", 0)
	_option(parts[2], main._ui_primary_btn, "주 사용 손", 1)
	if not _is_pc_profile():
		var more := _button(parts[2], "버튼 설정", Rect2(0, 240, 240, 58))
		more.button_down.connect(func():
			main.controller_mapper.check_toggle_ui()
		)

func _build_advanced(page: Control):
	var back := _button(page, "Connect", Rect2(0, 0, 160, 56), "arrow-left")
	back.button_down.connect(ui.switch_tab.bind(1))
	if _is_pc_profile():
		var title := _label(page, "연결 설정", Rect2(0, 88, 776, 40), 26, true)
		title.name = "ConnectionSettingsTitle"
		var body := Control.new()
		body.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_rect(body, page, Rect2(0, 156, 776, 196))
		_option(body, main._ui_reconnect_btn, "자동 재연결", 0)
		_option(body, main._ui_idle_btn, "미사용 시 연결 해제", 1)
		return
	var parts := _subpages(page, ["Reconnect", "Input"], ["자동 연결", "컨트롤러 입력"])
	# The ordinary-host input settings retain the original controls and behavior.
	back.position.x = 616
	back.add_theme_constant_override("h_separation", 8)
	back.add_theme_font_size_override("font_size", 22)
	page.move_child(back, -1)
	_option(parts[0], main._ui_reconnect_btn, "자동 재연결", 0)
	_option(parts[0], main._ui_idle_btn, "미사용 시 연결 해제", 1)
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
	for button in _tint_buttons:
		if is_instance_valid(button): button.text = _tint_buttons[button]
	if _tint_values.size() == 2:
		_tint_values[0].text = "%+d" % main.settings.host.picture_temperature
		_tint_values[1].text = "%+d" % main.settings.host.picture_tint
	if is_instance_valid(_stream_info):
		_stream_info.text = ui._stream_resolution_note.text
		_stream_info.tooltip_text = ui._stream_resolution_note.tooltip_text
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
