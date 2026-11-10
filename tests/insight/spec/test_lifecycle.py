import datetime as dt

import pytest
import yaml

from tuvl_insight.spec import lifecycle as lc

SPEC = "---\nname: refunds\nowner: ops\n---\n# Intent\nRefund customers.\n"
NOW = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC)


def front(text: str) -> dict:
    return lc.split_front(text)[0]


def test_a_new_spec_is_a_draft() -> None:
    state = lc.state_of(SPEC)
    assert state.status == "draft" and not state.stale and state.effective == "draft"


def test_approval_records_who_when_and_the_body_hash() -> None:
    approved = lc.approve(SPEC, by="Ada <a@b.c>", now=NOW)
    f = front(approved)
    assert f["status"] == "approved" and f["approved_by"] == "Ada <a@b.c>"
    assert f["approved_at"] == "2026-10-19T12:00:00Z"
    assert f["approved_hash"] == lc.body_hash(SPEC)
    assert f["owner"] == "ops"  # other front matter is kept
    assert lc.state_of(approved).effective == "approved"


def test_the_front_matter_is_not_part_of_the_approved_hash() -> None:
    approved = lc.approve(SPEC, by="a")
    edited = lc.with_front(approved, {"owner": "someone else"})
    assert lc.state_of(edited).effective == "approved"


def test_editing_the_body_makes_the_approval_stale() -> None:
    approved = lc.approve(SPEC, by="a")
    changed = approved.replace("Refund customers.", "Refund customers quickly.")
    state = lc.state_of(changed)
    assert state.status == "approved" and state.stale and state.effective == "draft"
    again = lc.approve(changed, by="b")  # a stale approval can be renewed
    assert lc.state_of(again).effective == "approved"


def test_approving_twice_or_a_superseded_spec_is_refused() -> None:
    approved = lc.approve(SPEC, by="a")
    with pytest.raises(lc.LifecycleError, match="already approved"):
        lc.approve(approved, by="a")
    with pytest.raises(lc.LifecycleError, match="superseded"):
        lc.approve(lc.with_front(SPEC, {"status": "superseded"}), by="a")


def test_reject_needs_a_reason_and_clears_the_approval() -> None:
    approved = lc.approve(SPEC, by="a")
    with pytest.raises(lc.LifecycleError, match="reason"):
        lc.reject(approved, by="b", reason="  ")
    rejected = lc.reject(approved, by="b", reason="missing the refund limit", now=NOW)
    f = front(rejected)
    assert f["status"] == "draft" and f["review_note"] == "missing the refund limit"
    assert "approved_hash" not in f and "approved_by" not in f
    assert "review_note" not in front(
        lc.approve(rejected, by="a")
    )  # a new approval clears the note


def test_reopen_only_applies_to_an_approved_spec() -> None:
    with pytest.raises(lc.LifecycleError, match="approved"):
        lc.reopen(SPEC)
    assert lc.state_of(lc.reopen(lc.approve(SPEC, by="a"))).status == "draft"


def test_the_body_is_kept_verbatim() -> None:
    approved = lc.approve(SPEC, by="a")
    assert lc.split_front(approved)[1] == lc.split_front(SPEC)[1]


def test_a_spec_without_front_matter_gets_one() -> None:
    approved = lc.approve("# Intent\nx\n", by="a")
    assert approved.startswith("---\n") and lc.state_of(approved).effective == "approved"


def test_bad_front_matter_is_an_error() -> None:
    with pytest.raises(lc.LifecycleError):
        lc.split_front("---\n: : [\n---\nbody\n")


def test_resolve_by_id_stem_name_and_path(tmp_path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    a = specs / "SPEC-SK-0004-01.md"
    a.write_text("---\nid: SPEC-SK-0004-01\nname: refunds\n---\n# x\n")
    b = specs / "other.md"
    b.write_text("# y\n")
    for ref in ("SPEC-SK-0004-01", "refunds"):
        assert lc.resolve_spec(tmp_path, ref) == a
    assert lc.resolve_spec(tmp_path, "specs/SPEC-SK-0004-01.md") == a.resolve()
    assert lc.resolve_spec(tmp_path, "other") == b
    assert lc.resolve_spec(tmp_path, "specs/other.md") == b.resolve()
    with pytest.raises(lc.LifecycleError, match="no spec"):
        lc.resolve_spec(tmp_path, "nope")


def test_idea_links_follow_the_specs_front_matter(tmp_path) -> None:
    from tuvl_insight.spec import ids

    ids.write_index(
        tmp_path, [{"id": "IDEA-SK-0004", "specs": []}, {"id": "IDEA-SK-0005", "specs": []}]
    )
    (tmp_path / "idea" / "IDEA-SK-0004").mkdir(parents=True)
    (tmp_path / "idea" / "IDEA-SK-0004" / "IDEA.yaml").write_text("id: IDEA-SK-0004\nspecs: []\n")
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "SPEC-SK-0004-01.md").write_text(
        "---\nid: SPEC-SK-0004-01\nideas: [IDEA-SK-0004]\n---\n# x\n"
    )
    (specs / "SPEC-SK-0004-02.md").write_text(
        "---\nid: SPEC-SK-0004-02\nideas: [IDEA-SK-0004, IDEA-SK-0005]\n---\n# x\n"
    )
    lc.sync_idea_links(tmp_path)
    rows = {r["id"]: r["specs"] for r in ids.read_index(tmp_path)}
    assert rows == {
        "IDEA-SK-0004": ["SPEC-SK-0004-01", "SPEC-SK-0004-02"],
        "IDEA-SK-0005": ["SPEC-SK-0004-02"],
    }
    record = yaml.safe_load((tmp_path / "idea" / "IDEA-SK-0004" / "IDEA.yaml").read_text())
    assert record["specs"] == ["SPEC-SK-0004-01", "SPEC-SK-0004-02"]
