extends SceneTree
func _initialize() -> void:
    var probe = ClassDB.instantiate("StreamRetirementProbe")
    var result = probe.run()
    assert(result.passed)
    await process_frame
    await process_frame
    var finished = probe.finish()
    assert(finished.passed)
    print(JSON.stringify({"stream_stop": result, "queued_delivery": finished}))
    probe = null
    quit(0)
