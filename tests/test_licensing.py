"""The project is Apache-2.0 everywhere it says so, and the notices that must ship do ship."""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: SHA-256 of the unmodified text published at apache.org/licenses/LICENSE-2.0.txt.
APACHE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
PACKAGES = (ROOT, ROOT / "insight")


def test_license_text_is_the_unmodified_apache_2_0() -> None:
    text = (ROOT / "LICENSE").read_bytes()
    assert b"Apache License\n                           Version 2.0, January 2004" in text
    assert hashlib.sha256(text).hexdigest() == APACHE_SHA256


def test_every_package_declares_apache_and_ships_its_notices() -> None:
    for package in PACKAGES:
        meta = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        assert meta["license"] == "Apache-2.0", package
        assert not [c for c in meta.get("classifiers", []) if c.startswith("License ::")], package
        for name in meta["license-files"]:
            assert (package / name).is_file(), f"{package.name}: {name} is missing"
    for name in ("LICENSE", "NOTICE"):  # tuvl-insight builds from its own directory
        assert (ROOT / name).read_bytes() == (PACKAGES[1] / name).read_bytes(), name


def test_bundled_fonts_carry_their_license() -> None:
    fonts = ROOT / "ui" / "public" / "fonts"
    assert "SIL OPEN FONT LICENSE Version 1.1" in (fonts / "OFL.txt").read_text()
    assert "Geist" in (ROOT / "NOTICE").read_text()


def test_the_ui_notices_cover_the_production_dependencies() -> None:
    notices = (PACKAGES[1] / "THIRD_PARTY_LICENSES.txt").read_text()
    assert notices.count("=" * 78) >= 2 * 100
    assert "react " in notices and "@xyflow/react" in notices


def test_no_stale_mit_statement_in_what_users_read() -> None:
    files = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "pyproject.toml",
    ]
    files += (ROOT / "docs").rglob("*.md")
    files += (ROOT / "src" / "tuvl").rglob("*.py")
    files += [ROOT / "ui" / "src" / "pages" / "SettingsPage.tsx"]
    stale = [str(p.relative_to(ROOT)) for p in files if re.search(r"\bMIT\b", p.read_text())]
    assert stale == []


def test_the_contribution_files_exist_and_point_at_each_other() -> None:
    for name in ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "DCO"):
        assert (ROOT / name).is_file(), name
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    for target in ("(DCO)", "(CODE_OF_CONDUCT.md)", "(SECURITY.md)", "(LICENSE)"):
        assert target in contributing, target
    assert "Developer's Certificate of Origin 1.1" in (ROOT / "DCO").read_text()


def test_no_contact_is_a_personal_address() -> None:
    for name in ("CODE_OF_CONDUCT.md", "SECURITY.md", "CONTRIBUTING.md", "README.md"):
        assert "sooraj@" not in (ROOT / name).read_text(), name
