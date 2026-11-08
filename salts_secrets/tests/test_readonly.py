from salts_secrets import readonly


def test_read_only_sessions_run_no_tool():
    assert readonly.on_pre_tool_call(tool_name="terminal", session_id="salt-v-abc")["action"] == "block"
    assert readonly.on_pre_tool_call(tool_name="memory", session_id="salt-v-abc", args={})["action"] == "block"
    assert readonly.on_pre_tool_call(tool_name="terminal", session_id="salt-o-abc") is None
    assert readonly.on_pre_tool_call(tool_name="terminal") is None
