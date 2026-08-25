"""Tests for the installer's version and build-stamp contract
(`mv3dt_installer.__version__`, `build_info()`, `build_stamp()`, and
`pyproject.toml`'s dynamic-version wiring; doc 00 section 4.1).

Run from installer/: `python3 -m pytest tests/test_version.py -v`

Never writes into the real package directory. The "this is a CI-stamped
release build" case is exercised by copying `mv3dt_installer/__init__.py`
into a throwaway package under `tmp_path` and dropping a `_buildinfo.py`
next to it, which is exactly what `.github/workflows/release.yml` does to
the real tree at build time. That keeps the working checkout clean while
still testing the shipped source of `__init__.py` rather than a paraphrase
of it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mv3dt_installer  # noqa: E402
from mv3dt_installer import __version__  # noqa: E402


PACKAGE_INIT = Path(mv3dt_installer.__file__).resolve()
PYPROJECT = PACKAGE_INIT.parent.parent / "pyproject.toml"

# The release workflow gates on `"v" + __version__ == $GITHUB_REF_NAME`, so
# the version has to be spellable as a tag: three dot-separated numbers,
# optionally followed by a pre-release suffix such as `0.2.0-rc.1`.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$")


def _make_package(root: Path, name: str, buildinfo: str | None = None) -> None:
    """Copy the real `__init__.py` into `root/name/`, optionally alongside a
    `_buildinfo.py` with the given contents."""
    package_dir = root / name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        PACKAGE_INIT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    if buildinfo is not None:
        (package_dir / "_buildinfo.py").write_text(buildinfo, encoding="utf-8")


def _import_package(root: Path, name: str, monkeypatch, frozen: bool = False):
    """Import `root/name` as a fresh top-level package and make sure it does
    not linger in `sys.modules` for the next test.

    `frozen` simulates the PyInstaller bootloader's `sys.frozen` attribute,
    which is what `__init__.py` actually gates a CI-written `_buildinfo.py`
    on; the real release workflow always runs the stamped import inside a
    frozen binary, so a test that wants "CI wrote this and it took effect"
    has to set it too, not just drop the file next to the package.
    """
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, f"{name}._buildinfo", raising=False)
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    module = __import__(name)
    return module


# ---------------------------------------------------------------------------
# __version__
# ---------------------------------------------------------------------------


def test_version_is_a_taggable_string():
    assert isinstance(__version__, str)
    assert VERSION_RE.match(__version__), (
        f"__version__ {__version__!r} cannot be turned into a `v`-prefixed "
        "release tag; release.yml's version gate would reject it"
    )


def test_version_has_no_leading_v():
    """The `v` belongs to the tag, not to the version. Storing it here would
    make the gate compare `vv0.1.0` against `v0.1.0`."""
    assert not __version__.startswith("v")


# ---------------------------------------------------------------------------
# build_info() / build_stamp()
# ---------------------------------------------------------------------------


def test_build_info_falls_back_when_buildinfo_absent(tmp_path, monkeypatch):
    _make_package(tmp_path, "unstamped_pkg")
    module = _import_package(tmp_path, "unstamped_pkg", monkeypatch)

    assert module.build_info() == ("", "source", "unknown")
    assert module.build_stamp() == ""


def test_build_info_reads_the_ci_written_buildinfo(tmp_path, monkeypatch):
    _make_package(
        tmp_path,
        "stamped_pkg",
        buildinfo=(
            'TAG = "v9.9.9"\n'
            'COMMIT = "a1b2c3d"\n'
            'BUILT_UTC = "2026-08-19T14:02:11Z"\n'
        ),
    )
    module = _import_package(tmp_path, "stamped_pkg", monkeypatch, frozen=True)

    assert module.build_info() == ("v9.9.9", "a1b2c3d", "2026-08-19T14:02:11Z")
    assert module.build_stamp() == (
        " (v9.9.9, commit a1b2c3d, built 2026-08-19T14:02:11Z)"
    )


def test_build_stamp_drops_an_empty_tag(tmp_path, monkeypatch):
    """A pull-request or branch build is stamped with a commit and a build
    time but no tag, because there is no release for it to name. The tag
    has to fall out of the suffix rather than leave a dangling comma."""
    _make_package(
        tmp_path,
        "untagged_pkg",
        buildinfo=(
            'TAG = ""\n'
            'COMMIT = "e215330"\n'
            'BUILT_UTC = "2026-08-19T22:19:36Z"\n'
        ),
    )
    module = _import_package(tmp_path, "untagged_pkg", monkeypatch, frozen=True)

    assert module.build_stamp() == " (commit e215330, built 2026-08-19T22:19:36Z)"


def test_build_stamp_composes_the_release_version_banner(tmp_path, monkeypatch):
    """The banner app.py's `--version` renders, assembled the same way."""
    _make_package(
        tmp_path,
        "banner_pkg",
        buildinfo=(
            'TAG = "v0.2.0"\n'
            'COMMIT = "a1b2c3d"\n'
            'BUILT_UTC = "2026-08-19T14:02:11Z"\n'
        ),
    )
    module = _import_package(tmp_path, "banner_pkg", monkeypatch, frozen=True)

    banner = f"mv3dt-installer {module.__version__}{module.build_stamp()}"
    assert banner == (
        f"mv3dt-installer {__version__} "
        "(v0.2.0, commit a1b2c3d, built 2026-08-19T14:02:11Z)"
    )


def test_buildinfo_present_but_unfrozen_still_reports_source_provenance(
    tmp_path, monkeypatch
):
    """A `_buildinfo.py` left behind by an earlier local `pyinstaller` build
    (an untracked, gitignored file that can easily survive a return to
    running from source) must not be picked up outside a frozen binary.
    This is what makes `test_installed_package_reports_source_provenance`
    below safe to assert unconditionally rather than skip."""
    _make_package(
        tmp_path,
        "leftover_buildinfo_pkg",
        buildinfo=(
            'TAG = "v9.9.9"\n'
            'COMMIT = "a1b2c3d"\n'
            'BUILT_UTC = "2026-08-19T14:02:11Z"\n'
        ),
    )
    module = _import_package(tmp_path, "leftover_buildinfo_pkg", monkeypatch)

    assert module.build_info() == ("", "source", "unknown")
    assert module.build_stamp() == ""


def test_installed_package_reports_source_provenance():
    """The test suite always runs unfrozen, so the real package must report
    itself as an unstamped source build regardless of whether an untracked
    `_buildinfo.py` happens to be sitting next to it from an earlier local
    PyInstaller build."""
    assert mv3dt_installer.build_info() == ("", "source", "unknown")
    assert mv3dt_installer.build_stamp() == ""


# ---------------------------------------------------------------------------
# pyproject.toml dynamic-version wiring
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict:
    tomllib = pytest.importorskip(
        "tomllib",
        reason="tomllib is 3.11+; the wiring is identical on the 3.10 floor",
    )
    with open(PYPROJECT, "rb") as f:
        return tomllib.load(f)


def test_pyproject_declares_the_version_dynamic():
    data = _load_pyproject()
    project = data["project"]

    assert "version" not in project, (
        "a hardcoded version here can drift from mv3dt_installer.__version__"
    )
    assert "version" in project["dynamic"]


def test_pyproject_points_the_dynamic_version_at_the_module_attribute():
    data = _load_pyproject()

    assert data["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "mv3dt_installer.__version__"
    }


def test_pyproject_dynamic_version_resolves_to_the_real_attribute():
    """Resolve the `attr` string the way setuptools does and confirm it
    lands on the value the release gate compares the tag against."""
    data = _load_pyproject()
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    module_name, _, attribute = attr.rpartition(".")

    module = __import__(module_name)
    assert getattr(module, attribute) == __version__
