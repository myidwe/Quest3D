class_name ProductTheme
extends RefCounted

const BG := Color("#15181D")
const PANEL := Color("#1D2229")
const RAISED := Color("#242A32")
const TEXT := Color("#F3F5F7")
const MUTED := Color("#A5AFBB")
const ACCENT := Color("#FFA18D")
const PALE := Color("#372A27")
const BORDER := Color("#39414B")
const FONT = preload("res://src/assets/precision/fonts/Pretendard-Regular.otf")
const MEDIUM = preload("res://src/assets/precision/fonts/Pretendard-Medium.otf")
const SEMIBOLD = preload("res://src/assets/precision/fonts/Pretendard-SemiBold.otf")

static func surface(color: Color, radius: int = 12, padding: int = 12) -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = color
	style.set_corner_radius_all(radius)
	style.set_content_margin_all(padding)
	return style

static func create() -> Theme:
	var theme := Theme.new()
	theme.default_font = FONT
	theme.default_font_size = 24
	for kind in ["Label", "Button", "LineEdit"]:
		theme.set_color("font_color", kind, TEXT)
	theme.set_color("font_hover_color", "Button", TEXT)
	theme.set_color("font_pressed_color", "Button", ACCENT)
	theme.set_color("font_disabled_color", "Button", MUTED)
	theme.set_stylebox("normal", "Button", surface(RAISED))
	theme.set_stylebox("hover", "Button", surface(Color("#303740")))
	theme.set_stylebox("pressed", "Button", surface(PALE))
	theme.set_stylebox("disabled", "Button", surface(Color("#1D2229")))
	var focus := surface(Color.TRANSPARENT)
	focus.border_color = ACCENT
	focus.set_border_width_all(2)
	theme.set_stylebox("focus", "Button", focus)
	var input := surface(RAISED)
	input.border_color = BORDER
	input.set_border_width_all(1)
	theme.set_stylebox("normal", "LineEdit", input)
	theme.set_color("caret_color", "LineEdit", ACCENT)
	return theme

static func primary(button: Button) -> void:
	button.add_theme_stylebox_override("normal", surface(PALE, 14))
	button.add_theme_stylebox_override("hover", surface(Color("#49322C"), 14))
	button.add_theme_stylebox_override("pressed", surface(Color("#5A3C33"), 14))
	button.add_theme_color_override("font_color", ACCENT)
	button.add_theme_color_override("font_hover_color", ACCENT)
	button.add_theme_color_override("font_pressed_color", ACCENT)

const ICON_TEXTURES := {
	"app-window": preload("res://src/assets/precision/icons/app-window.svg"),
	"arrow-left": preload("res://src/assets/precision/icons/arrow-left.svg"),
	"box": preload("res://src/assets/precision/icons/box.svg"),
	"chevron-down": preload("res://src/assets/precision/icons/chevron-down.svg"),
	"circle-check": preload("res://src/assets/precision/icons/circle-check.svg"),
	"circle-help": preload("res://src/assets/precision/icons/circle-help.svg"),
	"copy": preload("res://src/assets/precision/icons/copy.svg"),
	"external-link": preload("res://src/assets/precision/icons/external-link.svg"),
	"glasses": preload("res://src/assets/precision/icons/glasses.svg"),
	"link": preload("res://src/assets/precision/icons/link.svg"),
	"minus": preload("res://src/assets/precision/icons/minus.svg"),
	"monitor": preload("res://src/assets/precision/icons/monitor.svg"),
	"plus": preload("res://src/assets/precision/icons/plus.svg"),
	"power": preload("res://src/assets/precision/icons/power.svg"),
	"settings-2": preload("res://src/assets/precision/icons/settings-2.svg"),
	"sliders-horizontal": preload("res://src/assets/precision/icons/sliders-horizontal.svg"),
	"wifi": preload("res://src/assets/precision/icons/wifi.svg"),
	"x": preload("res://src/assets/precision/icons/x.svg"),
}
static var _icon_cache: Dictionary = {}

static func icon(name: String, accent: bool = false, raster_scale: float = 2.0) -> Texture2D:
	# ResourceLoader follows Godot's exported .svg.import remap. Raw SVG source
	# is absent in an APK; FileAccess would silently return no icon there.
	var cache_key := "%s:%s:%.2f" % [name, accent, raster_scale]
	if _icon_cache.has(cache_key): return _icon_cache[cache_key]
	var source_name := "arrow-left" if name in ["arrow-right", "arrow-up", "arrow-down"] else name
	if not ICON_TEXTURES.has(source_name): return null
	var image: Image = ICON_TEXTURES[source_name].get_image()
	if image == null or image.is_empty(): return null
	image = image.duplicate()
	if image.is_compressed() and image.decompress() != OK: return null
	image.convert(Image.FORMAT_RGBA8)
	if name == "arrow-right":
		image.rotate_90(CLOCKWISE)
		image.rotate_90(CLOCKWISE)
	elif name == "arrow-up": image.rotate_90(CLOCKWISE)
	elif name == "arrow-down": image.rotate_90(COUNTERCLOCKWISE)
	var color := ACCENT if accent else Color("#D8DEE6")
	for y in image.get_height():
		for x in image.get_width():
			image.set_pixel(x, y, Color(color.r, color.g, color.b, image.get_pixel(x, y).a))
	if raster_scale != 1.0:
		image.resize(maxi(1, roundi(image.get_width() * raster_scale)), maxi(1, roundi(image.get_height() * raster_scale)), Image.INTERPOLATE_LANCZOS)
	var texture := ImageTexture.create_from_image(image)
	_icon_cache[cache_key] = texture
	return texture

static func text(value: String) -> String:
	const WORDS := {
		"On":"On", "Off":"Off", "Auto":"Auto", "Flat":"Flat", "Black":"Black", "White":"White", "Low":"Low", "Medium":"Medium", "High":"High",
		"Passthrough":"Passthrough", "SBS":"SBS", "AI 3D":"3D Mode", "Curve":"Curve", "Background":"Background", "Ambient":"Ambient", "Colour":"Color", "Bezel":"Bezel",
		"Resolution":"Eye Resolution", "FPS":"FPS", "Bitrate":"Bitrate", "Host Cursor":"PC 마우스", "Codec":"Codec", "Quick Start":"빠른 연결",
		"Brightness":"Brightness", "Contrast":"Contrast", "Gamma":"Gamma", "Sharpen":"Sharpening", "Cursor Type":"포인터 모양", "Cursor Steady":"포인터 보정", "Tracking":"손 추적", "Double Click":"더블 클릭", "Mapping":"버튼 설정", "Device Mode":"기기 모드", "Alternate Mode":"전환 방식", "Primary Hand":"주 사용 손",
		"Auto-Reconnect":"자동 재연결", "Idle Disconnect":"미사용 시 연결 해제", "Circle":"원형", "Standard":"기본", "Right":"오른손", "Left":"왼손", "Head":"머리 방향",
		"Smaller":"화면 작게", "Larger":"화면 크게", "Nearer -0.25m":"더 가까이", "Farther +0.25m":"더 멀리", "PC 2D":"2D로 보기", "PC 3D":"3D로 보기", "Depth -":"Depth −", "Depth +":"Depth +",
		"Runtime":"Runtime", "Current":"Current", "Supersample":"Supersample", "Runtime Normal":"Runtime Normal", "Runtime Quality":"Runtime Quality", "Runtime Auto":"Runtime Auto", "Quality":"Quality", "Normal":"Normal",
		"Apply":"적용", "Save":"저장", "Remove":"삭제", "Ready":"준비됨", "Not connected":"PC 연결 대기", "Monitors":"Monitor", "Grid Mode":"격자 배치"
	}
	return WORDS.get(value, value)

static func stream_summary(value: String) -> String:
	return value.replace("Eye Unknown", "Eye 확인 중").replace("Idle", "연결 대기")
