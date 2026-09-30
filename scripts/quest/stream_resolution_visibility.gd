extends SceneTree

class QuietMain extends "res://main.gd":
	func _ready(): pass
	func _process(_delta): pass
	func _notification(_what): pass

class ObservedBackend extends StreamBackend:
	var output: Dictionary = {}
	func get_video_output_info() -> Dictionary: return output.duplicate(true)

func _init():
	_run.call_deferred()

func _run():
	var args := OS.get_cmdline_user_args()
	var evidence := args[0]
	var baseline := "baseline" in args
	var app = load("res://main.tscn").instantiate()
	app.set_script(QuietMain)
	root.add_child(app)
	app.screen_registry.initialize(app.screen_mesh)
	app.layout = ScreenLayout.new()
	app.screen_manager = ScreenManager.new(app)
	app.settings_controller = SettingsController.new(app)
	app.controller_mapper = ControllerMapper.new(app)
	app.add_child(app.controller_mapper)
	app.pc_control = PcControl.new(app)
	app.ui_controller = UIController.new(app)
	app.xr_interaction = XRInteraction.new(app)
	app.stream_backend = ObservedBackend.new(null)
	app.native_xr_renderer = NativeXrRendererManager.new(app)
	app.native_xr_renderer.active = true
	app.settings.host.pc_profile_resolution = Vector2i(3200, 900)
	app.ui_visible = true
	app.ui_panel_3d.visible = true
	app.ui_controller.build_ui()
	app.session_lifecycle.stream_started()
	app._last_hostname = "UI fixture"
	app.ui_controller.update_host_label()
	app.ui_controller.set_disconnect_visible(true)
	app.settings_controller.refresh_resolution_btn_label()
	app.stream_backend.output = {"observed": true, "valid": true, "codec_instance": 11,
		"codec_generation": 2, "width": 3200, "height": 928,
		"crop_left": 0, "crop_top": 0, "crop_right": 3199, "crop_bottom": 899,
		"transport_epoch": "a".repeat(32), "decoder_epoch": 7}
	app.ui_controller.switch_tab(1)
	await _draw()
	var quality_logs: int = app._log_lines.size()
	app.ui_controller._poll_stream_resolution()
	app.ui_controller._poll_stream_resolution()
	assert(app._log_lines.size() == quality_logs, "Unchanged diagnostics must not log every poll")
	assert(app._log_lines[-1].begins_with("[STREAM-QUALITY] Eye 1600x900 · 16:9 · Native"))
	assert("3200x928" in app._log_lines[-1] and not "\n" in app._log_lines[-1])
	var label: Label = app.ui_viewport.find_child("StreamResolutionObservation", true, false)
	var row: Control = app.ui_controller._tab_stream.get_node("StreamRow1")
	var panel: Control = app.ui_viewport.find_child("Panel", true, false)
	var handle: Control = app.ui_viewport.find_child("CompGrabBar", true, false)
	var normal: Image = app.ui_viewport.get_texture().get_image()
	assert(normal.save_png(evidence.path_join("stream.png")) == OK)
	var text: String = label.text
	label.text = ""
	await _draw()
	var blank: Image = app.ui_viewport.get_texture().get_image()
	label.text = text
	await _draw()
	var rect := Rect2i(label.get_global_rect())
	var changed := 0
	for y in range(rect.position.y, rect.end.y):
		for x in range(rect.position.x, rect.end.x):
			if normal.get_pixel(x, y) != blank.get_pixel(x, y):
				changed += 1
	var report := {"mode": "baseline" if baseline else "fixed", "text": text,
		"label_rect": str(rect), "font_size": label.get_theme_font_size("font_size"),
		"row1_rect": str(row.get_global_rect()), "drawn_changed_pixels": changed,
		"panel": str(panel.size), "handle": str(handle.get_global_rect()),
		"renderer": RenderingServer.get_video_adapter_name(),
		"scope": "Actual production Godot scene and GLES raster; injected decoder output, no Android/Quest runtime"}
	print(JSON.stringify(report))
	var proof := FileAccess.open(evidence.path_join("visibility.json"), FileAccess.WRITE)
	proof.store_string(JSON.stringify(report, "\t"))
	proof.close()
	if not baseline:
		assert(text == "Eye 1600x900 · 16:9 · Native")
		assert("3200x928" in label.tooltip_text and "3200x900" in label.tooltip_text)
		assert(changed > 500, "Actual rendered glyph pixels must exist")
		assert(label.get_theme_font_size("font_size") >= 26)
		assert(label.get_theme_font("font").get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, label.get_theme_font_size("font_size")).x <= label.size.x)
		assert(label.get_global_rect().end.y <= row.get_global_rect().position.y)
		for tab in range(7):
			app.ui_controller.switch_tab(tab)
			await _draw()
			assert(panel.size == Vector2(1200, 580))
			assert(handle.get_global_rect() == Rect2(300, 530, 600, 44))
			assert(panel.get_node("MenuLayout/VBox").get_combined_minimum_size().y <= 530)
		app.ui_controller.switch_tab(1)
		app.stream_backend.output = {}
		await label.get_child(0).timeout
		assert(label.text == "Eye Unknown · Native")
		assert(app._log_lines.size() == quality_logs + 1, "The next unknown observation logs exactly once")
		app.session_lifecycle.finish_cleanup()
		app.ui_controller._poll_stream_resolution()
		assert(label.text == "Eye Unknown · Idle")
	app.native_xr_renderer.active = false
	# The last await may resume inside Timer.timeout; leave that emission before
	# destroying its owning scene and the UI controller retained by its callback.
	await process_frame
	app.free()
	await process_frame
	print("Actual rendered Stream visibility PASS (" + ("baseline observation" if baseline else "fixed regression") + ")")
	quit()

func _draw():
	await process_frame
	await process_frame
	await RenderingServer.frame_post_draw
