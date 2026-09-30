extends SceneTree
func _init():
	var probe = ClassDB.instantiate("AudioStartupProbe")
	assert(probe != null)
	var result: Dictionary = probe.run()
	assert(result.passed)
	print(JSON.stringify(result))
	quit()
