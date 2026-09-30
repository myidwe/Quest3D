extends SceneTree

var errors: Array[int] = []
var messages: Array[String] = []
var states: Array[int] = []
var attempts: Array[int] = []
var failed := 0

func observe(stream: Node) -> void:
    stream.stream_terminated.connect(func(code: int, message: String):
        errors.append(code)
        messages.append(message))
    stream.state_changed.connect(func(state: int): states.append(state))
    stream.reconnect_attempt.connect(func(attempt: int, _maximum: int): attempts.append(attempt))
    stream.reconnect_failed.connect(func(): failed += 1)

func clear_events() -> void:
    errors.clear()
    messages.clear()
    states.clear()
    attempts.clear()
    failed = 0

func wait_state(stream: Node, expected: int) -> void:
    var limit := Time.get_ticks_msec() + 2000
    while stream.get_state() != expected and Time.get_ticks_msec() < limit:
        await process_frame
    assert(stream.get_state() == expected)

func assert_no_timer(stream: Node) -> void:
    for child in stream.get_children():
        assert(not child is Timer)

func _initialize() -> void:
    await process_frame
    var probe = ClassDB.instantiate("StreamStartRefusalProbe")
    var unready = ClassDB.instantiate("NightfallStream")
    observe(unready)
    assert(unready.start_stream("fixture", {}, {}) == -5605)
    assert(unready.get_state() == 3 and errors == [-5605])
    unready.free()
    assert(errors == [-5605]) # Destruction must not publish UI termination.
    clear_events()

    var stream = ClassDB.instantiate("NightfallStream")
    root.add_child(stream)
    observe(stream)
    stream.set_reconnect_delay_ms(100)
    stream.set_max_reconnect_attempts(2)

    probe.occupy()
    assert(stream.start_stream("fixture", {}, {}) == -5604)
    assert(stream.get_state() == 3 and errors == [-5604])
    assert(messages[0].contains("Another stream"))
    assert(attempts.is_empty() and failed == 0)
    probe.assert_other_running()
    probe.release()
    clear_events()

    assert(probe.start_normal(stream) == 0)
    await wait_state(stream, 2)
    probe.request_failure()
    await wait_state(stream, 4)
    # Preserve wrapper RECONNECTING while its old native owner fully stops.
    stream.get_stream_connection().stop()
    probe.occupy()
    await wait_state(stream, 3)
    await create_timer(0.35).timeout
    assert(errors[-1] == -5604 and failed == 1 and attempts == [1])
    assert_no_timer(stream)
    probe.assert_other_running()
    probe.release()
    stream.stop_stream()
    clear_events()

    # Accepted native retry failures retain the retry budget, rather than
    # resetting it to zero on every start_stream call.
    assert(probe.start_normal(stream) == 0)
    await wait_state(stream, 2)
    probe.request_failure()
    await wait_state(stream, 4)
    probe.fail_future_starts()
    await create_timer(0.8).timeout
    assert(stream.get_state() == 3 and failed == 1 and attempts == [1, 2])
    assert(errors == [-91, -92, -92])
    assert_no_timer(stream)
    stream.stop_stream()
    clear_events()

    assert(probe.start_normal(stream) == 0)
    await wait_state(stream, 2)
    probe.request_failure()
    await wait_state(stream, 4)
    probe.fail_drain()
    await wait_state(stream, 3)
    await create_timer(0.35).timeout
    assert(errors[-1] == -5602 and failed == 1 and attempts == [1])
    assert(messages[-1].contains("Restart the Quest app"))
    assert_no_timer(stream)
    clear_events()

    var fresh = ClassDB.instantiate("NightfallStream")
    root.add_child(fresh)
    observe(fresh)
    assert(fresh.start_stream("fixture", {}, {}) == -5602)
    assert(fresh.get_state() == 3 and errors == [-5602])
    assert(attempts.is_empty() and failed == 0)
    var state_count := states.size()
    fresh.free()
    stream.free()
    await process_frame
    assert(errors == [-5602] and states.size() == state_count)
    probe = null
    print(JSON.stringify({"passed": true, "actual_nightfall_stream": true,
        "actual_godot_retry_timer": true, "busy_initial_and_retry": true,
        "quarantined_initial_and_retry": true, "accepted_connects": true,
        "retry_budget_bounded": true, "destruction_silent": true,
        "actual_moonlight_network": false, "actual_quest": false}))
    quit(0)
