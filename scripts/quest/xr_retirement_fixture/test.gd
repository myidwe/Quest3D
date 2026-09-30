extends SceneTree
const Manager = preload("res://src/native_xr_renderer.gd")
class Owner extends Node3D:
	const PC_SBS_BUILD = false
	var stream_backend = null
	var depth_estimator = null
	var comp = null
func _init():
	call_deferred("run")

func run():
	var probe = ClassDB.instantiate("XrRetirementProbe")
	assert(probe != null)
	var result: Dictionary = probe.run()
	print(JSON.stringify(result))
	if "--baseline" in OS.get_cmdline_user_args():
		assert(result.premature_destroy)
	else:
		assert(not result.premature_destroy)
	# Object caller references can disappear while the raw provider is borrowed.
	var renderer = probe.begin_dispose(true)
	var object_id: int = renderer.get_instance_id()
	assert(not renderer is RefCounted)
	var owner = Owner.new()
	var manager = Manager.new(owner)
	manager.renderer = renderer
	manager.provider_registered = true
	renderer.shutdown_completed.connect(manager._on_native_shutdown_completed)
	owner.free() # Actual scene object is already gone before detached shutdown.
	manager.shutdown(false)
	assert(manager._shutdown_requested and not manager._shutdown_complete)
	assert(is_instance_valid(manager.renderer))
	manager.process_frame(false) # Must not dereference the detached main.
	manager = null
	renderer = null
	await process_frame
	await process_frame
	var status: Dictionary = probe.observe_dispose()
	assert(is_instance_id_valid(object_id) and status.valid and status.provider_registered)
	assert(status.destroyed == 0 and not status.status.complete)
	probe.end_dispose(0, 45) # Wrong cycle cannot authorize cleanup.
	await process_frame
	await process_frame
	assert(probe.observe_dispose().provider_registered)
	probe.end_dispose(0, 44)
	assert(probe.observe_dispose().retired and probe.observe_dispose().provider_registered)
	await process_frame
	await process_frame
	assert(not is_instance_id_valid(object_id))
	probe.finish_dispose(false)
	# An error end keeps both the Object and borrowed resources alive.
	renderer = probe.begin_dispose(true)
	assert(not renderer.request_dispose())
	renderer = null
	probe.end_dispose(-1, 44)
	await process_frame
	await process_frame
	status = probe.observe_dispose()
	assert(status.valid and status.provider_registered and status.destroyed == 0)
	assert(not status.status.complete and status.status.resource_failure != 0)
	probe.finish_dispose(true)
	# An off-owner request progresses only when the real Godot deferred queue
	# runs on the original owner. Ordinary owner Exit follows the same free path.
	for off_owner in [true, false]:
		renderer = probe.begin_dispose(false)
		if off_owner:
			probe.dispose_off_owner()
			assert(probe.observe_dispose().provider_registered)
		else:
			owner = Owner.new()
			root.add_child(owner)
			manager = Manager.new(owner)
			manager.renderer = renderer
			renderer.shutdown_completed.connect(manager._on_native_shutdown_completed)
			manager.shutdown()
			assert(manager._shutdown_complete and manager.renderer == null)
			owner.free()
			manager = null
			assert(not probe.observe_dispose().provider_registered)
		renderer = null
		await process_frame
		await process_frame
		probe.finish_dispose(false)
	print("Godot Object disposal: borrowed/wrong-end/success, failed-end retained, off-owner, ordinary Exit: PASS")
	quit()
