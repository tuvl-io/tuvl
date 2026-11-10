from tuvl_insight.coding_agents import Settings, read_settings, write_settings


def test_round_trip(tmp_path) -> None:
    assert read_settings(tmp_path) is None
    path = write_settings(tmp_path, Settings("SK", "claude"))
    assert path == tmp_path / ".tuvl" / "agent.yaml"
    assert read_settings(tmp_path) == Settings("SK", "claude")


def test_a_broken_file_reads_as_absent(tmp_path) -> None:
    (tmp_path / ".tuvl").mkdir()
    (tmp_path / ".tuvl" / "agent.yaml").write_text("agent: [")
    assert read_settings(tmp_path) is None
