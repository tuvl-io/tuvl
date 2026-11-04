from types import SimpleNamespace

from tuvl.runtime.loop import _assistant_message


def test_assistant_message_round_trips_tool_call_extra_content() -> None:
    message = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                id="c1",
                type="function",
                function=SimpleNamespace(name="lookup", arguments="{}"),
                extra_content={"google": {"thought_signature": "sig-123"}},
            )
        ],
    )

    assert _assistant_message(message) == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
                "extra_content": {"google": {"thought_signature": "sig-123"}},
            }
        ],
    }
