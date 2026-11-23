"""Exercise the upgrade notes CLI with synthetic checkouts and a captured plan."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/upgrade_notes.py"


def git(checkout, *args):
    return subprocess.check_output(["git", "-C", str(checkout), *args], encoding="utf-8").strip()


def checkout(path, project, *dependencies, entries=None):
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
    if entries:
        directory = path / "changelog.d"
        directory.mkdir()
        for filename, content in entries.items():
            (directory / filename).write_text(content, encoding="utf-8")
    git(path, "add", "--all")
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
        "Synthetic installation",
    )
    return git(path, "rev-parse", "HEAD")


def changelog_entry(title, notes="", *, summary="Synthetic summary."):
    text = f"---\ndate: 2026-11-21\ntype: changed\nimpact: medium\ntitle: {title}\n---\n{summary}\n"
    if notes:
        text += f"\n## Technical notes\n\n{notes}\n"
    return text


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


@pytest.fixture
def stored_file_counts():
    checked_counts = {
        "Report PDFs": 2,
        "Issued invoice PDFs": 0,
        "Subcontract source PDFs": 2,
        "EDD export ZIPs": 0,
        "Laboratory logos": 0,
        "Equipment event evidence": 0,
        "Training record evidence": 1,
        "Nonconformance event evidence": 0,
        "Total": 5,
    }
    return {
        category: {"checked": checked, "failures": 0}
        for category, checked in checked_counts.items()
    }


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


def test_reports_no_pending_migrations_or_version_or_note_changes(tmp_path):
    candidate = tmp_path / "candidate"
    checkout(candidate, "example-project", ("same-version", "4.0"), entries={"README.md": "Guide"})
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")

    result = run_notes(candidate, candidate, plan)

    assert result.returncode == 0, result.stderr
    assert "No migrations were pending on the populated baseline." in result.stdout
    assert "## Verified records" not in result.stdout
    assert "## Stored-file integrity" not in result.stdout
    assert "No locked dependency versions changed." in result.stdout
    assert "No technical notes were added or revised." in result.stdout
    assert "The baseline predates the changelog." not in result.stdout


def test_reports_verified_baseline_record_counts_in_category_order(tmp_path):
    candidate = tmp_path / "candidate"
    checkout(candidate, "example-project")
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    counts = tmp_path / "verified-counts.json"
    counts.write_text('{"QC measurements": 3, "custody events": 8}', encoding="utf-8")

    result = run_notes(candidate, candidate, plan, "--verified-counts", str(counts))

    assert result.returncode == 0, result.stderr
    assert (
        "## Verified records\n\n"
        "| Record category | Matched baseline rows |\n"
        "| --- | ---: |\n"
        "| custody events | 8 |\n"
        "| QC measurements | 3 |\n" in result.stdout
    )


def test_reports_counts_after_restore_and_upgrade(tmp_path):
    candidate = tmp_path / "candidate"
    checkout(candidate, "example-project")
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    restored = tmp_path / "restored-counts.json"
    restored.write_text('{"QC measurements": 3, "custody events": 8}', encoding="utf-8")
    verified = tmp_path / "verified-counts.json"
    verified.write_text('{"custody events": 8, "QC measurements": 3}', encoding="utf-8")

    result = run_notes(
        candidate,
        candidate,
        plan,
        "--restored-counts",
        str(restored),
        "--verified-counts",
        str(verified),
    )

    assert result.returncode == 0, result.stderr
    assert "CI restored a backup of the synthetic baseline" in result.stdout
    assert (
        "| Record category | After restore on baseline | After upgrade to candidate |\n"
        "| --- | ---: | ---: |\n"
        "| custody events | 8 | 8 |\n"
        "| QC measurements | 3 | 3 |\n" in result.stdout
    )


def test_reports_migrated_stored_file_counts_separately(tmp_path, stored_file_counts):
    candidate = tmp_path / "candidate"
    checkout(candidate, "example-project")
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    verified = tmp_path / "verified-counts.json"
    verified.write_text('{"custody events": 8}', encoding="utf-8")
    files = tmp_path / "stored-file-counts.json"
    files.write_text(json.dumps(stored_file_counts, sort_keys=True), encoding="utf-8")

    result = run_notes(
        candidate,
        candidate,
        plan,
        "--restored-counts",
        str(verified),
        "--verified-counts",
        str(verified),
        "--stored-file-counts",
        str(files),
    )

    assert result.returncode == 0, result.stderr
    assert (
        "| custody events | 8 | 8 |\n\n"
        "## Stored-file integrity\n\n"
        "After migrating the restored database, the candidate checked these files against\n"
        "their recorded SHA-256 and any recorded byte lengths. All checks passed.\n"
        "A zero means the restored database had no files in that category.\n\n"
        "| File category | Files checked after migration |\n"
        "| --- | ---: |\n"
        "| Report PDFs | 2 |\n"
        "| Issued invoice PDFs | 0 |\n"
        "| Subcontract source PDFs | 2 |\n"
        "| EDD export ZIPs | 0 |\n"
        "| Laboratory logos | 0 |\n"
        "| Equipment event evidence | 0 |\n"
        "| Training record evidence | 1 |\n"
        "| Nonconformance event evidence | 0 |\n"
        "| Total | 5 |\n\n"
        "## Django migrations" in result.stdout
    )


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        (
            {"Report PDFs |\n## Injected heading": {"checked": 0, "failures": 0}},
            "Expected stored-file counts for every known file category and Total.",
        ),
        (
            {"Report PDFs": {"checked": 2, "failures": 0, "diagnostic": "injected"}},
            "Expected nonnegative integer checked and failures counts in each row.",
        ),
        (
            {"Report PDFs": {"checked": True, "failures": 0}},
            "Expected nonnegative integer checked and failures counts in each row.",
        ),
        (
            {"Report PDFs": {"checked": 2, "failures": -1}},
            "Expected nonnegative integer checked and failures counts in each row.",
        ),
        (
            {"Total": {"checked": 6, "failures": 0}},
            "Stored-file totals must equal the sums of the category counts.",
        ),
        (
            {"Report PDFs": {"checked": 2, "failures": 1}},
            "Stored-file totals must equal the sums of the category counts.",
        ),
        (
            {"Report PDFs": {"checked": 2, "failures": 1}, "Total": {"checked": 5, "failures": 1}},
            "Stored-file verification reported failures.",
        ),
    ],
)
def test_rejects_invalid_stored_file_counts(tmp_path, stored_file_counts, updates, error):
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    files = tmp_path / "stored-file-counts.json"
    files.write_text(json.dumps(stored_file_counts | updates), encoding="utf-8")

    result = run_notes(tmp_path, tmp_path, plan, "--stored-file-counts", str(files))

    assert result.returncode != 0
    assert error in result.stderr
    assert "injected" not in result.stderr.casefold()
    assert not result.stdout


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ("[]", "Expected stored-file counts for every known file category and Total."),
        (
            '{"Total": {"checked": 0, "failures": 0}}',
            "Expected stored-file counts for every known file category and Total.",
        ),
        ('{"Total":', "Expecting value"),
        (
            '{"Total": {"checked": 0, "failures": 1, "failures": 0}}',
            "Stored-file counts contain a duplicate JSON key.",
        ),
    ],
)
def test_rejects_malformed_stored_file_counts(tmp_path, payload, error):
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    files = tmp_path / "stored-file-counts.json"
    files.write_text(payload, encoding="utf-8")

    result = run_notes(tmp_path, tmp_path, plan, "--stored-file-counts", str(files))

    assert result.returncode != 0
    assert error in result.stderr
    assert not result.stdout


def test_rejects_restored_counts_without_verified_counts(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    restored = tmp_path / "restored-counts.json"
    restored.write_text('{"QC measurements": 3}', encoding="utf-8")

    result = run_notes(tmp_path, tmp_path, plan, "--restored-counts", str(restored))

    assert result.returncode != 0
    assert "--restored-counts requires --verified-counts" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize(
    ("restored_counts", "error"),
    [
        (
            {"QC measurements": 2},
            "--restored-counts and --verified-counts must contain identical category counts",
        ),
        (
            {"custody events": 1},
            "--restored-counts and --verified-counts must contain identical category counts",
        ),
        (
            {"QC measurements": True},
            "Expected a JSON object mapping record categories to positive integers.",
        ),
    ],
)
def test_rejects_invalid_or_inconsistent_restored_counts(tmp_path, restored_counts, error):
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    restored = tmp_path / "restored-counts.json"
    restored.write_text(json.dumps(restored_counts), encoding="utf-8")
    verified = tmp_path / "verified-counts.json"
    verified.write_text('{"QC measurements": 1}', encoding="utf-8")

    result = run_notes(
        tmp_path,
        tmp_path,
        plan,
        "--restored-counts",
        str(restored),
        "--verified-counts",
        str(verified),
    )

    assert result.returncode != 0
    assert error in result.stderr
    assert not result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "{}",
        '{"": 1}',
        '{"QC | measurements": 1}',
        '{"QC measurements": 0}',
        '{"QC measurements": -1}',
        '{"QC measurements": true}',
        '{"QC measurements": 1.0}',
        '{"QC measurements": "1"}',
    ],
)
def test_rejects_invalid_verified_counts_before_emitting_notes(tmp_path, payload):
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    counts = tmp_path / "verified-counts.json"
    counts.write_text(payload, encoding="utf-8")

    result = run_notes(tmp_path, tmp_path, plan, "--verified-counts", str(counts))

    assert result.returncode != 0
    assert "Expected a JSON object mapping record categories to positive integers." in result.stderr
    assert not result.stdout


def test_reports_only_new_and_revised_committed_technical_notes(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    new = "2026-11-21-new.md"
    revised = "2026-11-21-revised.md"
    added = "2026-11-21-added.md"
    unchanged = "2026-11-21-unchanged.md"
    summary_only = "2026-11-21-summary-only.md"
    no_notes = "2026-11-21-no-notes.md"
    removed = "2026-11-21-removed.md"
    retired = "2026-11-21-retired.md"
    unchanged_entry = changelog_entry("Unchanged entry", "Existing migration detail.")
    checkout(
        baseline,
        "project",
        entries={
            revised: changelog_entry("Original title", "Old rollback instructions."),
            added: changelog_entry("Update deployment setting"),
            unchanged: unchanged_entry,
            summary_only: changelog_entry("Old summary title", "Same migration detail."),
            removed: changelog_entry("Deleted entry", "Obsolete settings."),
            retired: changelog_entry("Retired instructions", "Retired rollback advice."),
        },
    )
    candidate_sha = checkout(
        candidate,
        "project",
        entries={
            new: changelog_entry("New lockfile entry", "Refresh locked packages."),
            revised: changelog_entry(
                "Updated migration",
                "Keep the migrated schema.\n\nUse upgraded workers for issuance.",
            ),
            added: changelog_entry(
                "Update deployment setting", "Set the new environment variable."
            ),
            unchanged: unchanged_entry,
            summary_only: changelog_entry(
                "New summary title", "Same migration detail.", summary="An edited summary."
            ),
            no_notes: changelog_entry("Interface change only"),
            retired: changelog_entry("Retired instructions"),
            "README.md": "This guide is not an entry.",
        },
    )
    (baseline / "changelog.d" / revised).write_text(
        changelog_entry(
            "Updated migration", "Keep the migrated schema.\n\nUse upgraded workers for issuance."
        ),
        encoding="utf-8",
    )
    (candidate / "changelog.d" / revised).write_text("dirty entry", encoding="utf-8")
    (candidate / "changelog.d" / "2026-11-21-untracked.md").write_text(
        changelog_entry("Untracked entry", "Untracked instructions."), encoding="utf-8"
    )
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")

    result = run_notes(baseline, candidate, plan, "--repository", "example-org/fork")

    assert result.returncode == 0, result.stderr
    notes = result.stdout
    base = f"https://github.com/example-org/fork/blob/{candidate_sha}/changelog.d"
    assert "These notes were added or revised between the tested commits." in notes
    assert f'<h3><a href="{base}/{new}">New lockfile entry</a></h3>' in notes
    assert f'<h3><a href="{base}/{revised}">Updated migration</a></h3>' in notes
    assert f'<h3><a href="{base}/{added}">Update deployment setting</a></h3>' in notes
    assert "<p>Refresh locked packages.</p>" in notes
    assert "<p>Keep the migrated schema.</p>\n<p>Use upgraded workers for issuance.</p>" in notes
    assert "<p>Set the new environment variable.</p>" in notes
    for excluded in (
        "Old rollback instructions.",
        "Same migration detail.",
        "Retired rollback advice.",
        "Untracked instructions.",
        unchanged,
        summary_only,
        no_notes,
        removed,
        retired,
    ):
        assert excluded not in notes


def test_baseline_before_changelog_lists_candidate_notes_as_reference(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    checkout(baseline, "project")
    filename = "2026-11-21-rollback.md"
    content = changelog_entry("Keep the schema", "Older workers can still insert orders.")
    candidate_sha = checkout(candidate, "project", entries={filename: content})
    (baseline / "changelog.d").mkdir()
    (baseline / "changelog.d" / filename).write_text(content, encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")

    result = run_notes(baseline, candidate, plan)

    assert result.returncode == 0, result.stderr
    assert "The baseline predates the changelog." in result.stdout
    assert "for reference and may describe changes already in the baseline." in result.stdout
    assert "plan above for what Django applied during this rehearsal." in result.stdout
    assert "Older workers can still insert orders." in result.stdout
    assert f"/blob/{candidate_sha}/changelog.d/{filename}" in result.stdout


def test_changelog_preserves_query_text_and_technical_literals(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    checkout(baseline, "project")
    checkout(
        candidate,
        "project",
        entries={
            "2026-11-21-literal.md": changelog_entry(
                "See [details](https://example.invalid) & use <schema>",
                "Use www.example.invalid/run?a=1&b=2 with `CONFIG_VALUE`.\n"
                "<details>\n1. Run `migrate`.\n\n"
                "**Keep this literal.**",
            )
        },
    )
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")

    result = run_notes(baseline, candidate, plan)

    assert result.returncode == 0, result.stderr
    assert (
        ">See [details](https://example.invalid) &amp; use &lt;schema&gt;</a></h3>" in result.stdout
    )
    assert (
        "<blockquote>\n"
        "<p>Use www.example.invalid/run?a=1&amp;b=2 with `CONFIG_VALUE`."
        "<br>&lt;details&gt;<br>1. Run `migrate`.</p>\n"
        "<p>**Keep this literal.**</p>\n"
        "</blockquote>" in result.stdout
    )


@pytest.mark.parametrize("payload", ["{}", '["receipt.0014_example"]', '[["receipt", null]]'])
def test_malformed_plan_fails_before_emitting_notes(tmp_path, payload):
    plan = tmp_path / "plan.json"
    plan.write_text(payload, encoding="utf-8")

    result = run_notes(tmp_path, tmp_path, plan)

    assert result.returncode != 0
    assert "Expected a JSON array of [app_label, migration_name] pairs." in result.stderr
    assert not result.stdout
