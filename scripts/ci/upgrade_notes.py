"""Render notes after the populated upgrade rehearsal passes."""

import argparse
import json
import re
import subprocess
import tomllib
from collections import defaultdict
from pathlib import Path


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


def render_notes(baseline, candidate, repository, plan, rows):
    base_url = f"https://github.com/{repository}"
    before = f"{base_url}/blob/{baseline}"
    after = f"{base_url}/blob/{candidate}"
    ops = f"{after}/docs/operations.md"
    lines = [
        "# Populated upgrade rehearsal",
        "",
        "CI upgraded a disposable PostgreSQL database with synthetic records. It verified",
        "existing custody and audit events, requested tests, results and their revisions and",
        "worksheets, issued reports and files, and the invoice draft and its lines. The original",
        "and amended client PDF downloads matched their stored bytes.",
        "",
        "| Installation | Tested commit |",
        "| --- | --- |",
        f"| Baseline | [`{baseline}`]({base_url}/commit/{baseline}) |",
        f"| Candidate | [`{candidate}`]({base_url}/commit/{candidate}) |",
        "",
        f"[Compare the tested source trees]({base_url}/compare/{baseline}..{candidate})",
        "",
        "## Django migrations",
        "",
    ]
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
    lines += [
        "",
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
    parser.add_argument("--repository", default="openbga/environmental-lims", metavar="OWNER/REPO")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--repository must be a GitHub owner/repository name")

    try:
        plan = read_plan(args.plan)
        baseline = git_output(args.baseline, "rev-parse", "--verify", "HEAD").strip()
        candidate = git_output(args.candidate, "rev-parse", "--verify", "HEAD").strip()
        before = locked_versions(args.baseline, baseline)
        after = locked_versions(args.candidate, candidate)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))

    print(render_notes(baseline, candidate, args.repository, plan, dependency_rows(before, after)))


if __name__ == "__main__":
    main()
