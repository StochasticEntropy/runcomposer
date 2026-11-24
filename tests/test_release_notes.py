"""The release workflow takes its notes from CHANGELOG.md: the section from
the `## <version>` heading to the next `## `. Run here with the workflow's
own awk program, on this version — with its date or still `— unreleased`."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import runcomposer

from .conftest import REPO


def _awk_program() -> str:
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    found = re.search(r"awk -v ver=\"\$version\" '\n(.*?)\n\s*' CHANGELOG\.md", workflow, re.S)
    assert found, "the workflow extracts the notes with awk"
    return found.group(1)


@pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")
@pytest.mark.parametrize("heading_tail", ["unreleased", "2026-12-01"])
def test_the_workflow_finds_this_versions_section(tmp_path, heading_tail):
    version = runcomposer.__version__
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(rf"^## {re.escape(version)} — (.+)$", changelog, re.M)
    assert heading, f"CHANGELOG.md has a section for {version}"
    copy = tmp_path / "CHANGELOG.md"
    copy.write_text(changelog.replace(heading.group(0), f"## {version} — {heading_tail}", 1), encoding="utf-8")
    notes = subprocess.run(
        ["awk", "-v", f"ver={version}", _awk_program(), str(copy)], capture_output=True, text=True, check=True
    ).stdout
    assert notes.strip() and "\n## " not in notes
    assert notes.lstrip().startswith(changelog.split(heading.group(0), 1)[1].lstrip()[:60])

