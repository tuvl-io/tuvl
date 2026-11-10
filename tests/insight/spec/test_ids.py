import pytest
import yaml

from tuvl.contract.schema import Task
from tuvl_insight.spec import ids


def test_formats_and_derivation() -> None:
    idea = ids.idea_id("SK", 4)
    spec = ids.spec_id(idea, 1)
    task = ids.task_id(spec, 3)
    assert (idea, spec, task) == ("IDEA-SK-0004", "SPEC-SK-0004-01", "TASK-SK-0004-01-003")
    assert ids.idea_of(task) == ids.idea_of(spec) == idea
    assert ids.spec_of(task) == spec


def test_widths_are_minimums() -> None:
    assert ids.idea_id("SK", 12345) == "IDEA-SK-12345"
    assert ids.IDEA_ID.match("IDEA-SK-12345")
    assert ids.task_id("SPEC-SK-0004-123", 1000) == "TASK-SK-0004-123-1000"


@pytest.mark.parametrize(
    "bad", ["IDEA-sk-0004", "IDEA-S-0004", "IDEA-SK-4", "IDEA-SK-0004-01", "idea-SK-0004"]
)
def test_malformed_idea_ids_do_not_match(bad: str) -> None:
    assert ids.IDEA_ID.match(bad) is None


def test_the_schema_accepts_both_task_id_forms() -> None:
    pattern = Task.model_fields["id"].metadata[0].pattern
    assert pattern == ids.TASK_ID_PATTERN
    for ok in ("T1", "T12", "TASK-SK-0004-01-003"):
        Task(id=ok, title="t", type="manual")
    for bad in ("1", "TASK-SK-4-1-1", "task-SK-0004-01-003", "T"):
        with pytest.raises(ValueError):
            Task(id=bad, title="t", type="manual")


def test_numbering_is_per_initials_and_never_reuses(tmp_path) -> None:
    assert ids.next_idea_id(tmp_path, "SK") == "IDEA-SK-0001"
    ids.write_index(
        tmp_path, [{"id": "IDEA-SK-0001"}, {"id": "IDEA-SK-0003"}, {"id": "IDEA-AL-0009"}]
    )
    assert ids.next_idea_id(tmp_path, "SK") == "IDEA-SK-0004"
    assert ids.next_idea_id(tmp_path, "AL") == "IDEA-AL-0010"
    (tmp_path / "idea" / "IDEA-SK-0007").mkdir()  # a folder alone also counts
    assert ids.next_idea_id(tmp_path, "SK") == "IDEA-SK-0008"


def test_spec_ids_follow_their_idea(tmp_path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "a.md").write_text("---\nid: SPEC-SK-0004-01\n---\n# A\n")
    (specs / "SPEC-SK-0004-02.md").write_text("# B\n")
    assert ids.next_spec_id(tmp_path, "IDEA-SK-0004") == "SPEC-SK-0004-03"
    assert ids.next_spec_id(tmp_path, "IDEA-SK-0005") == "SPEC-SK-0005-01"
    assert set(ids.spec_ids(tmp_path)) == {"SPEC-SK-0004-01", "SPEC-SK-0004-02"}


def test_duplicates_are_found(tmp_path) -> None:
    ids.write_index(tmp_path, [{"id": "IDEA-SK-0001"}, {"id": "IDEA-SK-0001"}])
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "x.md").write_text("---\nid: SPEC-SK-0001-01\n---\n# X\n")
    (specs / "y.md").write_text("---\nid: SPEC-SK-0001-01\n---\n# Y\n")
    assert sorted(d.id for d in ids.duplicates(tmp_path)) == ["IDEA-SK-0001", "SPEC-SK-0001-01"]


def test_token_finds_ids_in_text_but_not_inside_longer_words() -> None:
    text = "see IDEA-SK-0004, SPEC-SK-0004-01 and TASK-SK-0004-01-003; not XIDEA-SK-0004 or IDEA-SK-00045x"
    found = ids.TOKEN.findall(text)
    assert found[:3] == ["IDEA-SK-0004", "SPEC-SK-0004-01", "TASK-SK-0004-01-003"]
    assert "XIDEA-SK-0004" not in found


def test_index_round_trip_keeps_order(tmp_path) -> None:
    rows = [{"id": "IDEA-SK-0002", "title": "b"}, {"id": "IDEA-SK-0001", "title": "a"}]
    ids.write_index(tmp_path, rows)
    assert ids.read_index(tmp_path) == rows
    assert yaml.safe_load((tmp_path / "idea" / "INDEX.yaml").read_text())["kind"] == "IdeaIndex"
