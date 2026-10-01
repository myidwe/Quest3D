extends SceneTree

var checks := 0
var failures: Array[String] = []

func check(value: bool, message: String):
	checks += 1
	if not value:
		failures.append(message)
		printerr("ICON FAIL ", message)

func _init():
	_run.call_deferred()

func _run():
	var theme = load("res://src/product_theme.gd")
	for name in theme.ICON_TEXTURES.keys() + ["arrow-right", "arrow-up", "arrow-down"]:
		var texture: Texture2D = theme.icon(name)
		check(texture != null, str(name) + " loads imported resource")
		if texture == null: continue
		check(texture == theme.icon(name), str(name) + " reuses cached texture")
		var image := texture.get_image()
		var opaque := 0
		var transparent := 0
		for y in image.get_height():
			for x in image.get_width():
				var alpha := image.get_pixel(x, y).a
				if alpha > 0.5: opaque += 1
				if alpha < 0.01: transparent += 1
		check(opaque > 20 and transparent > 20, str(name) + " has visible strokes and transparent background")
		var selected: Texture2D = theme.icon(name, true)
		check(selected != texture and selected == theme.icon(name, true), str(name) + " selection tint cached separately")
	check(theme.icon("not-an-icon") == null, "Unknown icon remains null")
	check(theme.icon("arrow-left").get_image().get_data() != theme.icon("arrow-right").get_image().get_data(), "Directional arrows have distinct orientation")
	print("PRODUCT_ICONS ", checks, " checks, ", failures.size(), " failures; raw SVG present=", FileAccess.file_exists("res://src/assets/precision/icons/arrow-left.svg"))
	quit(0 if failures.is_empty() else 1)
