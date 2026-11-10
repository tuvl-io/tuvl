import io
import zipfile
from pathlib import Path

import pytest
import yaml

from tuvl_insight.spec import ids
from tuvl_insight.spec.ideas import IdeaError, extract_text, import_idea, plan_import

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(path: Path, paragraphs: list[tuple[str, str]], extra: str = "") -> Path:
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
        for style, text in paragraphs
    )
    xml = f'{extra}<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    path.write_bytes(buf.getvalue())
    return path


def text_pdf(path: Path, text: str) -> Path:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path


@pytest.fixture
def brief(tmp_path: Path) -> Path:
    d = tmp_path / "brief"
    (d / "sub").mkdir(parents=True)
    (d / "overview.md").write_text("# Refund automation\n\nCustomers get refunds.\n")
    (d / "sub" / "notes.txt").write_text("notes\n")
    (d / ".hidden").write_text("x")
    (d / ".env").write_text("SECRET=1")
    (d / "server.pem").write_text("-----BEGIN-----")
    (d / "binary.exe").write_bytes(b"MZ")
    (d / ".git").mkdir()
    (d / ".git" / "config").write_text("x")
    return d


def test_the_plan_skips_secrets_hidden_and_unsupported_files(brief: Path) -> None:
    plan = plan_import([brief])
    assert [f.relative for f in plan.files] == ["overview.md", "sub/notes.txt"]
    reasons = dict(plan.skipped)
    assert "secret" in reasons[".env"] and "secret" in reasons["server.pem"]
    assert "unsupported" in reasons["binary.exe"]
    assert ".git/config" not in reasons


def test_size_limits_and_empty_imports_are_refused(brief: Path, tmp_path: Path) -> None:
    with pytest.raises(IdeaError, match="over the"):
        plan_import([brief], max_total=10)
    only_secret = tmp_path / "s"
    only_secret.mkdir()
    (only_secret / ".env").write_text("x")
    with pytest.raises(IdeaError, match="nothing to import"):
        plan_import([only_secret])
    with pytest.raises(IdeaError, match="does not exist"):
        plan_import([tmp_path / "nope"])


def test_include_and_exclude(brief: Path) -> None:
    assert [f.relative for f in plan_import([brief], include=["*.md"]).files] == ["overview.md"]
    assert [f.relative for f in plan_import([brief], exclude=["*.txt"]).files] == ["overview.md"]


def test_a_symlink_is_not_followed(brief: Path, tmp_path: Path) -> None:
    secret = tmp_path / "outside.md"
    secret.write_text("outside")
    (brief / "link.md").symlink_to(secret)
    plan = plan_import([brief])
    assert "link.md" not in [f.relative for f in plan.files]
    assert dict(plan.skipped)["link.md"] == "symbolic link"


def test_import_writes_the_idea_record_and_index(brief: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    done = import_idea(
        project, plan_import([brief]), initials="SK", title=None, supersedes=None, imported_by="a"
    )
    assert done.id == "IDEA-SK-0001" and done.title == "Refund automation"
    folder = project / "idea" / "IDEA-SK-0001"
    assert (folder / "source" / "overview.md").read_text().startswith("# Refund")
    record = yaml.safe_load((folder / "IDEA.yaml").read_text())
    assert [f["path"] for f in record["files"]] == ["source/overview.md", "source/sub/notes.txt"]
    assert len(record["files"][0]["sha256"]) == 64
    assert [r["id"] for r in ids.read_index(project)] == ["IDEA-SK-0001"]
    second = import_idea(
        project,
        plan_import([brief]),
        initials="SK",
        title="Again",
        supersedes="IDEA-SK-0001",
        imported_by="a",
    )
    assert second.id == "IDEA-SK-0002"
    assert ids.read_index(project)[1]["supersedes"] == "IDEA-SK-0001"


def test_superseding_an_unknown_idea_is_refused(brief: Path, tmp_path: Path) -> None:
    with pytest.raises(IdeaError, match="not an idea"):
        import_idea(
            tmp_path,
            plan_import([brief]),
            initials="SK",
            title="t",
            supersedes="IDEA-SK-0099",
            imported_by="a",
        )
    assert not (tmp_path / "idea").exists()


def test_word_text_is_extracted_with_headings(tmp_path: Path) -> None:
    path = docx(tmp_path / "b.docx", [("Heading1", "Goals"), ("Normal", "Cut refund time.")])
    text, warning = extract_text(path)
    assert warning is None and text == "# Goals\n\nCut refund time."


def test_a_word_file_declaring_entities_is_refused(tmp_path: Path) -> None:
    path = docx(tmp_path / "b.docx", [("Normal", "x")], extra='<!DOCTYPE d [<!ENTITY a "aaaa">]>')
    text, warning = extract_text(path)
    assert text is None and "entities" in warning  # type: ignore[operator]


def test_broken_word_files_are_reported_not_raised(tmp_path: Path) -> None:
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    assert "could not be read" in extract_text(bad)[1]  # type: ignore[operator]


def test_pdf_text_is_extracted(tmp_path: Path) -> None:
    text, warning = extract_text(text_pdf(tmp_path / "b.pdf", "Refunds within two days"))
    assert warning is None and "Refunds within two days" in text  # type: ignore[operator]


def test_a_pdf_without_a_text_layer_warns(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    path = tmp_path / "scan.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    text, warning = extract_text(path)
    assert text is None and "no text layer" in warning  # type: ignore[operator]


def test_extracted_text_lands_next_to_the_source(tmp_path: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    docx(src / "brief.docx", [("Heading1", "Refund automation"), ("Normal", "Body")])
    text_pdf(src / "spec.pdf", "Hello")
    project = tmp_path / "p"
    project.mkdir()
    done = import_idea(
        project, plan_import([src]), initials="SK", title=None, supersedes=None, imported_by="a"
    )
    folder = project / "idea" / done.id
    assert (folder / "extracted" / "brief.docx.md").read_text().startswith("# Refund automation")
    assert "Hello" in (folder / "extracted" / "spec.pdf.md").read_text()
    assert done.title == "Refund automation"
    entry = {f["path"]: f for f in yaml.safe_load((folder / "IDEA.yaml").read_text())["files"]}
    assert entry["source/brief.docx"]["extracted"] == "extracted/brief.docx.md"


def test_a_failed_import_leaves_nothing_behind(brief: Path, tmp_path: Path, monkeypatch) -> None:
    import tuvl_insight.spec.ideas as module

    def boom(_path: Path) -> tuple[None, None]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(module, "extract_text", boom)
    project = tmp_path / "p"
    project.mkdir()
    with pytest.raises(RuntimeError):
        import_idea(
            project,
            plan_import([brief]),
            initials="SK",
            title=None,
            supersedes=None,
            imported_by="a",
        )
    assert not (project / "idea" / "IDEA-SK-0001").exists()
    assert ids.read_index(project) == []
