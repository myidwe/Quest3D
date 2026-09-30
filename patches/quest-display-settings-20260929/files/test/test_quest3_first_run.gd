extends SceneTree

# Real StateManager load/save paths and SettingsPersistence codecs, with only
# rendering/UI dependencies replaced. Run with isolated XDG_DATA_HOME.
class FakeController extends RefCounted:
	var STREAM_FPS_RATES: Array = [30, 40, 60, 72, 90, 120]
	var ai_3d_models: Array = [0, 1, 2, 3, 4]
	var choices: Array = [0, 6, 7]
	func get_sharpen_choices() -> Array: return choices
	func refresh_color_balance_controls(): pass
	func apply_filter(): pass
	func apply_depth_gpu_priority(_enabled): pass
	func normalize_ai_3d_model_for_type(): pass
	func enforce_ai3d_platform_lock(): pass
	func refresh_resolution_btn_label(): pass
	func apply_stereo(): pass
	func apply_screen_layout(_layout): pass
class FakeScreen extends RefCounted:
	func apply_curvature(): pass
	# View persistence is exercised with the real ScreenManager in
	# test_display_view_settings; this fixture isolates first-run defaults.
	func restore_view_options(_value): pass
	func view_options() -> Dictionary: return {"version": 1}
class FakeUi extends RefCounted:
	func update_stereo_shader(): pass
	func update_option_btn(_button, _label): pass
	func update_monitor_tab(): pass
class FakeMapper extends RefCounted:
	func write_settings(_save): pass
	func read_settings(_save): pass
	func is_input_mapping_supported() -> bool: return true
class FakePairStream extends RefCounted:
	var app: Node3D
	var fps_at_pair := 0
	var pin := "4321"
	func _init(owner: Node3D): app = owner
	func begin_pair(_ip: String, _port: int) -> String:
		fps_at_pair = app.settings.host.stream_fps
		return pin
class TestWelcome extends WelcomeScreen:
	func show_welcome_screen(_screen: String): pass
class FakeMain extends Node3D:
	var PC_SBS_BUILD := true
	var device_is_quest3 := true
	const SHARPEN_RUNTIME_NORMAL := 6
	var settings := AppSettings.new()
	var settings_controller := FakeController.new()
	var screen_manager := FakeScreen.new()
	var ui_controller: Variant = null
	var controller_mapper: Variant = null
	var stream_backend: Variant = null
	var stream_manager: Variant = null
	var state_manager: Variant = null
	var _pair_pin := ""
	var passthrough_supported := false
	var curvature := 2
	var layout := ScreenLayout.single(Vector2i(1920, 1080))
	var screens: Array = []
	var resolution_scale_options: Array = [100]
	var resolutions: Array = [Vector2i(1280, 720), Vector2i(1920, 1080)]
	var host_resolution := Vector2i(1920, 1080)
	var _ui_fps_btn: Variant = null
	var _ui_bitrate_btn: Variant = null
	var bitrate_labels: Array = ["Auto", "20"]
	var depth_estimator: Variant = null
	var comp: Variant = null
	func _log(_message): pass
	func compute_requested_resolution() -> Vector2i: return host_resolution
	func parse_ip_port(ip: String) -> Array: return [ip, 47989]
class TestState extends StateManager:
	func sync_ui_to_settings(): pass
	func save_host_state(): pass

var failures: Array[String] = []
var cases: Array[String] = []

func _init(): _run.call_deferred()

func _check(ok: bool, message: String):
	if not ok:
		failures.append(message)
		printerr("REGRESSION FAIL: " + message)

func _clear_files():
	for name in ["app_state.cfg", "host_state.cfg"]:
		var path: String = "user://" + name
		if FileAccess.file_exists(path):
			assert(DirAccess.remove_absolute(path) == OK)

func _main() -> FakeMain:
	var app := FakeMain.new()
	root.add_child(app)
	return app

func _text_file(path: String, content: String):
	var file := FileAccess.open(path, FileAccess.WRITE)
	assert(file != null)
	file.store_string(content)
	file.close()

func _read(path: String) -> String: return FileAccess.get_file_as_string(path)

func _run():
	# Explicitly fail rather than touch a normal developer or device profile.
	assert(OS.get_environment("NIGHTFALL_FIRST_RUN_FIXTURE") == "1")
	_clear_files()
	var app := _main()
	TestState.new(app).load_state()
	_check(app.settings.codec_preference == 1, "fresh Quest 3 selects HEVC")
	_check(app.settings.quick_start_enabled, "fresh Quest 3 enables Quick Start")
	_check(app.settings.host.stream_fps == 72, "fresh Pair path gets 72 FPS before host lookup")
	_check(app.settings.sharpen_mode == 6, "fresh Quest 3 uses supported Runtime Normal")
	_check(app.settings.brightness_pct == 0 and app.settings.contrast_pct == 100 and app.settings.gamma_pct == 100, "fresh picture remains neutral")
	_check(not FileAccess.file_exists("user://app_state.cfg"), "loading defaults does not eagerly write app settings")
	cases.append("fresh_quest3")
	app.free()

	app = _main()
	app.settings_controller.choices = [0]
	TestState.new(app).load_state()
	_check(app.settings.sharpen_mode == 0, "unsupported Runtime Normal retains Off")
	app.free()
	cases.append("unsupported_sharpen")
	for non_quest3 in [true, false]:
		app = _main()
		app.device_is_quest3 = not non_quest3
		app.PC_SBS_BUILD = non_quest3
		TestState.new(app).load_state()
		_check(app.settings.codec_preference == 0 and not app.settings.quick_start_enabled and app.settings.host.stream_fps == 30 and app.settings.sharpen_mode == 0, "Quest 2/other build defaults preserved")
		app.free()
	cases.append("other_device_and_build")

	var config := ConfigFile.new()
	var saved := AppSettings.new()
	saved.codec_preference = 0
	saved.quick_start_enabled = false
	saved.sharpen_mode = 7
	SettingsPersistence.write_app(config, saved)
	assert(config.save("user://app_state.cfg") == OK)
	var bytes_before := _read("user://app_state.cfg")
	app = _main()
	var state := TestState.new(app)
	state.load_state()
	_check(app.settings.codec_preference == 0 and not app.settings.quick_start_enabled and app.settings.sharpen_mode == 7, "saved app choices preserved")
	_check(_read("user://app_state.cfg") == bytes_before, "saved app bytes preserved")
	app.settings.host.stream_fps = 40
	state.load_host_state("192.0.2.1")
	_check(app.settings.host.stream_fps == 72, "missing host file uses Quest 3 stream default")
	_check(app.settings.codec_preference == 0 and not app.settings.quick_start_enabled, "new host does not override saved global choices")
	config = ConfigFile.new()
	config.set_value("192.0.2.2", "fps", 40)
	assert(config.save("user://host_state.cfg") == OK)
	bytes_before = _read("user://host_state.cfg")
	app.settings.host.stream_fps = 40
	state.load_host_state("192.0.2.1")
	_check(app.settings.host.stream_fps == 72 and _read("user://host_state.cfg") == bytes_before, "new section default preserves other host document")
	app.ui_controller = FakeUi.new()
	state.load_host_state("192.0.2.2")
	_check(app.settings.host.stream_fps == 40, "saved host FPS preserved")
	app.free()
	cases.append("saved_app_and_host_selection")

	_clear_files()
	_text_file("user://app_state.cfg", "")
	app = _main()
	TestState.new(app).load_state()
	_check(app.settings.codec_preference == 0 and not app.settings.quick_start_enabled, "existing empty app file is not fresh install")
	app.free()
	cases.append("empty_existing_app")

	_clear_files()
	var invalid := "[screen\ncodec_preference=2\n"
	_text_file("user://app_state.cfg", invalid)
	app = _main()
	app.settings.codec_preference = 2
	app.settings.host.stream_fps = 40
	app.controller_mapper = FakeMapper.new()
	state = TestState.new(app)
	state.load_state()
	_check(app.settings.codec_preference == 2 and not app.settings.quick_start_enabled and app.settings.host.stream_fps == 40, "malformed app is not fresh install")
	state.save_state()
	_check(_read("user://app_state.cfg") == invalid, "automatic save preserves malformed app bytes")
	app.free()
	cases.append("malformed_app_preservation")

	_text_file("user://host_state.cfg", invalid)
	app = _main()
	app.settings.host.stream_fps = 40
	var input := LineEdit.new()
	input.name = "IPInput"
	app.add_child(input)
	input.owner = app
	input.unique_name_in_owner = true
	input.text = "192.0.2.1"
	var real_state := StateManager.new(app)
	real_state.load_host_state(input.text)
	_check(app.settings.host.stream_fps == 40, "malformed host does not receive fresh defaults")
	real_state.save_host_state()
	_check(_read("user://host_state.cfg") == invalid, "automatic save preserves malformed host bytes")
	app.free()
	cases.append("malformed_host_preservation")

	_clear_files()
	app = _main()
	app.settings.host.stream_fps = 40
	app.settings.codec_preference = 0
	app.settings.quick_start_enabled = false
	input = LineEdit.new()
	input.name = "IPInput"
	app.add_child(input)
	input.owner = app
	input.unique_name_in_owner = true
	app.state_manager = TestState.new(app)
	app.stream_manager = FakePairStream.new(app)
	var welcome := TestWelcome.new(app)
	welcome.start_pair("192.0.2.3")
	_check(app.stream_manager.fps_at_pair == 72, "direct IP Pair loads new-host default before native pairing")
	_check(app._pair_pin == "4321", "direct IP Pair still shows returned PIN")
	_check(app.settings.codec_preference == 0 and not app.settings.quick_start_enabled, "direct IP Pair preserves saved global choices")
	_check(not FileAccess.file_exists("user://host_state.cfg"), "direct IP Pair does not eagerly create host config")
	config = ConfigFile.new()
	config.set_value("192.0.2.3", "fps", 40)
	assert(config.save("user://host_state.cfg") == OK)
	bytes_before = _read("user://host_state.cfg")
	app.ui_controller = FakeUi.new()
	app.stream_manager.pin = ""
	welcome.start_pair("192.0.2.3")
	_check(app.stream_manager.fps_at_pair == 40, "failed direct IP Pair uses saved host FPS")
	_check(_read("user://host_state.cfg") == bytes_before, "failed direct IP Pair preserves saved host bytes")
	app.free()
	cases.append("direct_ip_pair_defaults_and_failure")

	print(JSON.stringify({"cases": cases, "failures": failures, "pass": failures.is_empty()}))
	quit(0 if failures.is_empty() else 1)
