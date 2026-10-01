class_name PrecisionDepthSlider
extends Button

signal depth_selected(percent: float)
var percent := 0.0
var dragging := false
var minimum := 0.0
var maximum := 4.0
var step := 0.05

func _ready():
	mouse_filter = Control.MOUSE_FILTER_STOP
	flat = true
	focus_mode = Control.FOCUS_NONE
	for state in ["normal", "hover", "pressed", "disabled"]:
		add_theme_stylebox_override(state, StyleBoxEmpty.new())
	gui_input.connect(_input_depth)

func _input_depth(event: InputEvent):
	if disabled:
		dragging = false
		return
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT:
		if event.pressed:
			dragging = true
			_preview(event.position.x)
		elif dragging:
			_preview(event.position.x)
			dragging = false
			depth_selected.emit(percent)
	elif event is InputEventMouseMotion and dragging:
		_preview(event.position.x)

func _preview(x: float):
	var t := clampf((x - 16.0) / maxf(1.0, size.x - 32.0), 0.0, 1.0)
	# Reach geometry bounds even when they are not a multiple of the step.
	# Keep the existing fine step everywhere between the two endpoints.
	percent = minimum if t <= 0.0 else (maximum if t >= 1.0 else clampf(snappedf(lerpf(minimum, maximum, t), step), minimum, maximum))
	queue_redraw()

func set_percent(value: float):
	if not dragging:
		percent = clampf(value, minimum, maximum)
		queue_redraw()

func _draw():
	var left := Vector2(16, size.y / 2.0)
	var right := Vector2(size.x - 16, size.y / 2.0)
	var thumb := left.lerp(right, clampf((percent - minimum) / maxf(0.001, maximum - minimum), 0.0, 1.0))
	draw_line(left, right, ProductTheme.BORDER, 10, true)
	draw_circle(left, 5, ProductTheme.BORDER)
	draw_circle(right, 5, ProductTheme.BORDER)
	draw_line(left, thumb, ProductTheme.ACCENT if not disabled else ProductTheme.MUTED, 10, true)
	draw_circle(left, 5, ProductTheme.ACCENT if not disabled else ProductTheme.MUTED)
	draw_circle(thumb + Vector2(0, 2), 18, Color(0.2, 0.25, 0.3, 0.08))
	draw_circle(thumb, 16, ProductTheme.TEXT)
	draw_arc(thumb, 16, 0, TAU, 48, Color("#D7DCE2"), 1, true)

func _notification(what: int):
	if what == NOTIFICATION_VISIBILITY_CHANGED and not is_visible_in_tree():
		dragging = false
