from tuvl_insight.coding_agents import AGENTS, detect, get, initials_from, valid_initials


def test_every_agent_has_a_unique_id_and_a_documented_invocation() -> None:
    assert len({a.id for a in AGENTS}) == len(AGENTS)
    assert all(a.headless and a.docs and a.binaries for a in AGENTS)
    assert get("claude").verified and not get("aider").verified  # type: ignore[union-attr]


def test_nothing_found_on_an_empty_path(tmp_path) -> None:
    assert detect(str(tmp_path)) == []


def test_finds_installed_agents_with_their_versions(agent_bin) -> None:
    agent_bin("claude", version="2.1.0 (Claude Code)")
    agent_bin("codex", version="codex-cli 0.9")
    found = {d.spec.id: d for d in detect(agent_bin.dir)}
    assert set(found) == {"claude", "codex"}
    assert found["claude"].version == "2.1.0 (Claude Code)"
    assert found["codex"].path.endswith("/codex")


def test_a_generic_executable_name_must_identify_itself(agent_bin) -> None:
    agent_bin("agent", version="1.0", help_text="some other tool")
    assert detect(agent_bin.dir) == []
    agent_bin("agent", version="1.0", help_text="Cursor Agent - run the agent")
    assert [d.spec.id for d in detect(agent_bin.dir)] == ["cursor"]


def test_the_older_cursor_name_is_found_too(agent_bin) -> None:
    agent_bin("cursor-agent", version="0.5", help_text="Cursor Agent")
    assert [d.spec.id for d in detect(agent_bin.dir)] == ["cursor"]


def test_initials() -> None:
    assert initials_from("Ada Lovelace") == "AL"
    assert initials_from("ada  grace hopper lovelace") == "AGH"
    assert initials_from("") == ""
    assert initials_from("X") == ""  # one letter is not a usable id part
    assert valid_initials("SK") and valid_initials("A1B2")
    assert not valid_initials("s") and not valid_initials("1AB") and not valid_initials("ABCDE")
