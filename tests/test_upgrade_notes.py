"""Exercise the upgrade notes CLI with synthetic lockfiles and a captured plan."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/upgrade_notes.py"


def git(checkout, *args):
    return subprocess.check_output(["git", "-C", str(checkout), *args], encoding="utf-8").strip()


def checkout(path, project, *dependencies):
    path.mkdir()
    git(path, "init", "--quiet")
    packages = [(project, "0.1.0", 'virtual = "."')]
    packages.extend(
        (name, version, 'registry = "https://example.invalid/simple"')
        for name, version in dependencies
    )
    lock = "version = 1\n"
    for name, version, source in packages:
        lock += f'\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ {source} }}\n'
    (path / "uv.lock").write_text(lock, encoding="utf-8")
    git(path, "add", "uv.lock")
    git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "-m",
        "Synthetic lockfile",
    )
    return git(path, "rev-parse", "HEAD")


def run_notes(baseline, candidate, plan, *args):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--plan",
            str(plan),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_reports_exact_endpoints_ordered_plan_and_locked_version_sets(tmp_path):
    baseline = tmp_path / "baseline checkout"
    candidate = tmp_path / "candidate checkout"
    old = checkout(
        baseline,
        "old-project",
        ("multi", "2.1"),
        ("multi", "1.4"),
        ("removed", "1.0"),
        ("same-version", "4.0"),
    )
    new = checkout(
        candidate,
        "new-project",
        ("added", "5.1"),
        ("multi", "3.0"),
        ("multi", "2.1"),
        ("same-version", "4.0"),
        ("same-version", "4.0"),
    )
    # The comparison uses the commits, even if the candidate worktree is dirty.
    (candidate / "uv.lock").write_text("uncommitted", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps([["receipt", "0014_example"], ["auth", "0013_example"]]), encoding="utf-8"
    )

    result = run_notes(baseline, candidate, plan, "--repository", "example-org/fork")

    assert result.returncode == 0, result.stderr
    notes = result.stdout
    url = "https://github.com/example-org/fork"
    assert f"{url}/commit/{old}" in notes
    assert f"{url}/commit/{new}" in notes
    assert f"{url}/compare/{old}..{new}" in notes
    assert f"{url}/blob/{new}/CHANGELOG.md" in notes
    assert f"{url}/blob/{new}/docs/operations.md#recover-production" in notes
    assert (
        f"{url}/blob/{new}/docs/operations.md#acceptance-run-after-installation-and-upgrade"
        in notes
    )
    assert "1. `receipt.0014_example`\n2. `auth.0013_example`" in notes
    assert "| `added` | Added | — | `5.1` |" in notes
    assert "| `multi` | Changed | `1.4`, `2.1` | `2.1`, `3.0` |" in notes
    assert "| `removed` | Removed | `1.0` | — |" in notes
    for package in ("old-project", "new-project", "same-version"):
        assert f"`{package}`" not in notes


def test_reports_no_pending_migrations_or_version_changes(tmp_path):
    candidate = tmp_path / "candidate"
    checkout(candidate, "example-project", ("same-version", "4.0"))
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")

    result = run_notes(candidate, candidate, plan)

    assert result.returncode == 0, result.stderr
    assert "No migrations were pending on the populated baseline." in result.stdout
    assert "No locked dependency versions changed." in result.stdout


@pytest.mark.parametrize("payload", ["{}", '["receipt.0014_example"]', '[["receipt", null]]'])
def test_malformed_plan_fails_before_emitting_notes(tmp_path, payload):
    plan = tmp_path / "plan.json"
    plan.write_text(payload, encoding="utf-8")

    result = run_notes(tmp_path, tmp_path, plan)

    assert result.returncode != 0
    assert "Expected a JSON array of [app_label, migration_name] pairs." in result.stderr
    assert not result.stdout
