extends SceneTree
func _init():
	var probe = ClassDB.instantiate("AudioCleanupProbe")
	assert(probe != null)
	var result: Dictionary = probe.run()
	assert(result.passed)
	print(JSON.stringify(result))
	quit()
