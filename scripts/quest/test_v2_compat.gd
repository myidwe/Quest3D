extends SceneTree

class V2ComputerManager extends RefCounted:
	var commands: Array = []
	func wire(path: String, method := "get", body: Dictionary = {}) -> Dictionary:
		var folder := OS.get_environment("QUEST_V2_TLS_ROOT")
		var output: Array = []
		var payload := "{}"
		if method == "post":
			var path_to_body := folder + "/godot-request-" + str(Time.get_ticks_usec()) + ".json"
			var file := FileAccess.open(path_to_body, FileAccess.WRITE)
			assert(file != null)
			file.store_string(JSON.stringify(body))
			file.close()
			payload = "@" + path_to_body # Preserve JSON quotes through OS.execute.
		var result := OS.execute(OS.get_environment("QUEST_CACHE") + "/tls-tests/tls-client",
			[OS.get_environment("QUEST_V2_TLS_URL") + path, folder + "/server.pem", folder + "/client.pem",
			folder + "/client.key", method, payload], output, true)
		assert(result == 0 and not output.is_empty())
		var response := String(output[0])
		var split := response.find("\n")
		assert(split > 0)
		return {"code": int(response.left(split)), "text": response.substr(split + 1).strip_edges()}
	func request_pc_control(_host: int, body: Dictionary, callback: Callable):
		commands.append(body.duplicate(true))
		var response := wire("/quest3d/v1/control", "post", body)
		callback.call(response.code, JSON.parse_string(response.text), "")

class ProfileStream extends StreamManager:
	var resized := Vector2i.ZERO
	func resize_stream_viewport(w: int, h: int): resized = Vector2i(w, h)

func _init(): _run.call_deferred()

func _run():
	var cm := V2ComputerManager.new()
	var serverinfo := cm.wire("/serverinfo")
	assert(serverinfo.code == 200)
	var parser := XMLParser.new()
	assert(parser.open_buffer(serverinfo.text.to_utf8_buffer()) == OK)
	var values := {}
	var node := ""
	while parser.read() == OK:
		if parser.get_node_type() == XMLParser.NODE_ELEMENT: node = parser.get_node_name()
		elif parser.get_node_type() == XMLParser.NODE_TEXT: values[node] = parser.get_node_data()
	assert(values.PairStatus == "1" and not values.has("Quest3DSourceKind"))
	var app = load("res://main.gd").new()
	app.stream_backend = StreamBackend.new(null)
	app.stream_backend.set_computer_manager(cm)
	app.settings_controller = SettingsController.new(app)
	app.ui_controller = UIController.new(app)
	var stream := ProfileStream.new(app)
	assert(stream._apply_pc_profile({"quest3d_profile_version": int(values.Quest3DProfileVersion),
		"quest3d_layout": values.Quest3DLayout, "width": int(values.Quest3DWidth), "height": int(values.Quest3DHeight)}))
	assert(stream.resized == Vector2i(2560, 720) and app.stream_backend._pc_frame_input_required)
	var control := PcControl.new(app)
	control._host = 1
	control._generation = 1
	for mode in ["3d", "2d"]:
		var before := cm.wire("/quest3d/v1/status")
		var status: Dictionary = JSON.parse_string(before.text)
		assert(before.code == 200 and not status.input_enabled and not status.has("source_kind"))
		control._on_status(before.code, status, "", 1)
		assert(control.available() and not control.pointer.enabled and not control.pointer.actual_input)
		control.set_mode(mode)
		assert(not control.pending.is_empty(), "202 from the real TLS fixture is accepted, not applied")
		var after := cm.wire("/quest3d/v1/status")
		control._on_status(after.code, JSON.parse_string(after.text), "", 1)
		assert(control.pending.is_empty() and control.status.effective_mode == mode)
		assert(not control.pointer.enabled and not control.pointer.actual_input)
	assert(cm.commands.size() == 2 and app.settings.host.effective_sbs_mode() == 3)
	assert(stream._apply_pc_profile({}) and not app.stream_backend._pc_frame_input_required)
	app.free()
	print("V2 compatibility real TLS PASS: pinned mTLS, profile, PC3D then PC2D, actual input OFF, ordinary Sunshine profile")
	quit(0)
