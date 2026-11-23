"""Render notes after the populated upgrade rehearsal passes."""

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.changelog import parse_entry, sort_key  # noqa: E402

STORED_FILE_CATEGORIES = (
    "Report PDFs",
    "Issued invoice PDFs",
    "Subcontract source PDFs",
    "EDD export ZIPs",
    "Laboratory logos",
    "Equipment event evidence",
    "Training record evidence",
    "Nonconformance event evidence",
)


def git_output(checkout, *args):
    return subprocess.check_output(["git", "-C", str(checkout), *args], encoding="utf-8")


def locked_versions(checkout, commit):
    lock = tomllib.loads(git_output(checkout, "show", f"{commit}:uv.lock"))
    versions = defaultdict(set)
    for package in lock["package"]:
        source = package["source"]
        if any(source.get(kind) == "." for kind in ("virtual", "editable", "directory")):
            continue  # The checkout itself is not a dependency.
        versions[package["name"]].add(package["version"])
    return versions


def changelog_blobs(checkout, commit):
    """Index committed entry files by blob ID without opening unchanged entries."""
    has_catalog = False
    blobs = {}
    tree = git_output(
        checkout, "ls-tree", "-r", "-t", "-z", "--full-tree", commit, "--", "changelog.d"
    )
    for record in tree.split("\0"):
        if not record:
            continue
        metadata, filename = record.split("\t", 1)
        mode, kind, blob = metadata.split()
        if kind == "tree" and filename == "changelog.d":
            has_catalog = True
        directory, _, name = filename.rpartition("/")
        if (
            mode.startswith("100")
            and directory == "changelog.d"
            and name.endswith(".md")
            and name.lower() != "readme.md"
        ):
            blobs[name] = blob
    return has_catalog, blobs


def changed_technical_notes(baseline_checkout, baseline, candidate_checkout, candidate):
    has_baseline_catalog, before = changelog_blobs(baseline_checkout, baseline)
    _, after = changelog_blobs(candidate_checkout, candidate)
    entries = []
    for name, blob in after.items():
        old_blob = before.get(name)
        if blob == old_blob:
            continue
        entry = parse_entry(name, text=git_output(candidate_checkout, "cat-file", "blob", blob))
        if not entry.notes:
            continue
        if old_blob:
            old = parse_entry(
                name, text=git_output(baseline_checkout, "cat-file", "blob", old_blob)
            )
            if old.notes == entry.notes:
                continue
        entries.append(entry)
    return has_baseline_catalog, sorted(entries, key=sort_key)


def read_plan(path):
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(part, str) and part for part in pair)
        for pair in plan
    ):
        raise ValueError("Expected a JSON array of [app_label, migration_name] pairs.")
    return plan


def read_verified_counts(path):
    counts = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(counts, dict)
        or not counts
        or any(
            not re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+)*", label)
            or type(count) is not int
            or count <= 0
            for label, count in counts.items()
        )
    ):
        raise ValueError("Expected a JSON object mapping record categories to positive integers.")
    return counts


def read_stored_file_counts(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Stored-file counts contain a duplicate JSON key.")
            result[key] = value
        return result

    counts = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(counts, dict) or counts.keys() != {*STORED_FILE_CATEGORIES, "Total"}:
        raise ValueError("Expected stored-file counts for every known file category and Total.")

    fields = {"checked", "failures"}
    for row in counts.values():
        if (
            not isinstance(row, dict)
            or row.keys() != fields
            or any(type(value) is not int or value < 0 for value in row.values())
        ):
            raise ValueError(
                "Expected nonnegative integer checked and failures counts in each row."
            )

    if any(
        counts["Total"][field]
        != sum(counts[category][field] for category in STORED_FILE_CATEGORIES)
        for field in fields
    ):
        raise ValueError("Stored-file totals must equal the sums of the category counts.")
    if counts["Total"]["failures"]:
        raise ValueError("Stored-file verification reported failures.")
    return counts


def format_versions(versions):
    return ", ".join(f"`{version}`" for version in sorted(versions)) or "—"


def dependency_rows(before, after):
    rows = []
    for name in sorted(before.keys() | after.keys()):
        old = before.get(name, set())
        new = after.get(name, set())
        if old == new:
            continue
        if not old:
            change = "Added"
        elif not new:
            change = "Removed"
        else:
            change = "Changed"
        rows.append(f"| `{name}` | {change} | {format_versions(old)} | {format_versions(new)} |")
    return rows


def render_notes(
    baseline,
    candidate,
    repository,
    plan,
    rows,
    has_baseline_catalog,
    entries,
    verified_counts=None,
    restored_counts=None,
    stored_file_counts=None,
):
    base_url = f"https://github.com/{repository}"
    before = f"{base_url}/blob/{baseline}"
    after = f"{base_url}/blob/{candidate}"
    ops = f"{after}/docs/operations.md"
    lines = [
        "# Populated upgrade rehearsal",
        "",
    ]
    if restored_counts:
        lines += [
            "CI restored a backup of the synthetic baseline into a separate PostgreSQL database.",
            "It verified that database on the baseline version before upgrading it to the",
            "candidate and verifying it again. Both checks covered",
        ]
    else:
        lines.append(
            "CI upgraded a disposable PostgreSQL database with synthetic records. It verified"
        )
    lines += [
        "existing custody and audit events, requested tests, results and their revisions and",
        "worksheets, issued reports and files, and the invoice draft and its lines.",
        "The order, sample and container records matched the baseline. For a corrected",
        "sample ID, the original receipt, the correction and the custody transfer stayed linked",
        "to the sample.",
        "It also verified QC samples, observations and corrective actions. The correction and",
        "the reanalysis disposition still linked to the original failed observation.",
        "For the subcontracted test, staff downloaded the original and corrected source PDFs.",
        "Both matched their stored bytes, and each issued version cited its reviewed result.",
        "The original and amended client PDF downloads matched their stored bytes.",
    ]
    if verified_counts and "electronic deliverable exports" in verified_counts:
        lines.append(
            "The owning client downloaded the stored exploratory electronic data deliverable "
            "(EDD) ZIP. Its bytes, frozen recipient mappings and issued report version "
            "matched the baseline. A user from another client could not download it."
        )
    lines += [
        "",
        "| Installation | Tested commit |",
        "| --- | --- |",
        f"| Baseline | [`{baseline}`]({base_url}/commit/{baseline}) |",
        f"| Candidate | [`{candidate}`]({base_url}/commit/{candidate}) |",
        "",
        f"[Compare the tested source trees]({base_url}/compare/{baseline}..{candidate})",
        "",
    ]
    if verified_counts:
        lines += ["## Verified records", ""]
        categories = sorted(verified_counts, key=lambda label: (label.casefold(), label))
        if restored_counts:
            lines += [
                "Each column shows how many rows matched the original baseline at that stage.",
                "",
                "| Record category | After restore on baseline | After upgrade to candidate |",
                "| --- | ---: | ---: |",
            ]
            lines += [
                f"| {label} | {restored_counts[label]} | {verified_counts[label]} |"
                for label in categories
            ]
        else:
            lines += ["| Record category | Matched baseline rows |", "| --- | ---: |"]
            lines += [f"| {label} | {verified_counts[label]} |" for label in categories]
        lines.append("")
    if stored_file_counts is not None:
        lines += [
            "## Stored-file integrity",
            "",
            "After migrating the restored database, the candidate checked these files against",
            "their recorded SHA-256 and any recorded byte lengths. All checks passed.",
            "A zero means the restored database had no files in that category.",
            "",
            "| File category | Files checked after migration |",
            "| --- | ---: |",
        ]
        lines += [
            f"| {category} | {stored_file_counts[category]['checked']} |"
            for category in (*STORED_FILE_CATEGORIES, "Total")
        ]
        lines.append("")
    lines += ["## Django migrations", ""]
    if plan:
        lines += ["Django applied these migrations in order.", ""]
        lines += [f"{index}. `{app}.{name}`" for index, (app, name) in enumerate(plan, start=1)]
    else:
        lines.append("No migrations were pending on the populated baseline.")
    lines += [
        "",
        "## Locked Python dependencies",
        "",
        "The comparison covers dependency version sets across the platforms and groups in",
        "`uv.lock`. Installed packages depend on the environment and selected groups.",
        f"[Baseline lockfile]({before}/uv.lock) · [Candidate lockfile]({after}/uv.lock)",
        "",
    ]
    if rows:
        lines += [
            "| Package | Change | Baseline versions | Candidate versions |",
            "| --- | --- | --- | --- |",
        ]
        lines += rows
    else:
        lines.append("No locked dependency versions changed.")
    lines += ["", "## Changelog technical notes", ""]
    if not has_baseline_catalog:
        lines += [
            "The baseline predates the changelog. The candidate's technical notes are listed",
            "for reference and may describe changes already in the baseline. See the migration",
            "plan above for what Django applied during this rehearsal.",
            "",
        ]
        if not entries:
            lines += ["The candidate contains no technical notes.", ""]
    elif entries:
        lines += ["These notes were added or revised between the tested commits.", ""]
    else:
        lines += ["No technical notes were added or revised.", ""]
    for entry in entries:
        entry_url = f"{after}/changelog.d/{entry.id}.md"
        # HTML blocks preserve the changelog's plain text without parsing it as Markdown.
        title = escape(entry.title, quote=False)
        lines += [f'<h3><a href="{entry_url}">{title}</a></h3>', "", "<blockquote>"]
        for paragraph in re.split(r"\n(?:[ \t]*\n)+", entry.notes):
            text = escape(paragraph, quote=False).replace("\n", "<br>")
            lines.append(f"<p>{text}</p>")
        lines += ["</blockquote>", ""]
    lines += [
        "## Deployment references",
        "",
        f"- [Candidate changelog]({after}/CHANGELOG.md)",
        f"- [Take a backup]({ops}#take-a-backup)",
        f"- [Recover production]({ops}#recover-production)",
        f"- [Run acceptance checks after upgrading]({ops}"
        "#acceptance-run-after-installation-and-upgrade)",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path, help="baseline Git checkout")
    parser.add_argument("--candidate", required=True, type=Path, help="candidate Git checkout")
    parser.add_argument("--plan", required=True, type=Path, help="rehearsed migration plan JSON")
    parser.add_argument(
        "--verified-counts", type=Path, help="matched baseline record counts after upgrade (JSON)"
    )
    parser.add_argument(
        "--restored-counts", type=Path, help="matched baseline record counts after restore (JSON)"
    )
    parser.add_argument(
        "--stored-file-counts", type=Path, help="stored-file verification after migration (JSON)"
    )
    parser.add_argument("--repository", default="openbga/environmental-lims", metavar="OWNER/REPO")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--repository must be a GitHub owner/repository name")
    if args.restored_counts and not args.verified_counts:
        parser.error("--restored-counts requires --verified-counts")

    try:
        plan = read_plan(args.plan)
        verified_counts = (
            read_verified_counts(args.verified_counts) if args.verified_counts else None
        )
        restored_counts = (
            read_verified_counts(args.restored_counts) if args.restored_counts else None
        )
        if restored_counts is not None and restored_counts != verified_counts:
            raise ValueError(
                "--restored-counts and --verified-counts must contain identical category counts"
            )
        stored_file_counts = (
            read_stored_file_counts(args.stored_file_counts) if args.stored_file_counts else None
        )
        baseline = git_output(args.baseline, "rev-parse", "--verify", "HEAD").strip()
        candidate = git_output(args.candidate, "rev-parse", "--verify", "HEAD").strip()
        before = locked_versions(args.baseline, baseline)
        after = locked_versions(args.candidate, candidate)
        has_baseline_catalog, entries = changed_technical_notes(
            args.baseline, baseline, args.candidate, candidate
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))

    print(
        render_notes(
            baseline,
            candidate,
            args.repository,
            plan,
            dependency_rows(before, after),
            has_baseline_catalog,
            entries,
            verified_counts=verified_counts,
            restored_counts=restored_counts,
            stored_file_counts=stored_file_counts,
        )
    )


if __name__ == "__main__":
    main()
