import json
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import Error as DatabaseError
from django.db import connection

from core.management.commands._stored_file_manifest import _unique_keys

# One statement gives the six aggregates the same snapshot even in autocommit mode.
_SUMMARY_SQL = """
    SELECT 'audit', count(*), max(id::text COLLATE "C") FROM core_auditevent
    UNION ALL SELECT 'custody', count(*), max(id::text COLLATE "C") FROM receipt_custodyevent
    UNION ALL SELECT 'result_revisions', count(*), max(id)::text FROM analytical_resultrevision
    UNION ALL SELECT 'report_versions', count(*), max(id)::text FROM reporting_reportversion
    UNION ALL SELECT 'report_files', count(*), NULL FROM reporting_reportfile
    UNION ALL SELECT 'issued_invoices', count(*), max(id)::text FROM commercial_issuedinvoice
"""
_UUID_GROUPS = {"audit", "custody"}
_GROUPS = _UUID_GROUPS | {"result_revisions", "report_versions", "report_files", "issued_invoices"}
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_MAX_BIGINT = 2**63 - 1


def _valid_max_id(group, count, value):
    if count == 0 or group == "report_files":
        return value is None
    if not isinstance(value, str):
        return False
    if group in _UUID_GROUPS:
        return _UUID.fullmatch(value) is not None
    if not re.fullmatch(r"0|-?[1-9][0-9]{0,18}", value):
        return False
    return -(2**63) <= int(value) <= _MAX_BIGINT


def _load_counts(path):
    # The emitted summary is under 1 KiB. Catch a dump selected by mistake before loading it.
    limit = 64 * 1024
    try:
        with Path(path).open("rb") as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise CommandError("The saved record summary exceeds 64 KiB.")
        summary = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except OSError:
        raise CommandError("Could not read the saved record summary.") from None
    except ValueError, RecursionError:
        raise CommandError(
            "The saved record summary must be UTF-8 JSON with unique keys."
        ) from None

    if (
        not isinstance(summary, dict)
        or set(summary) != {"version", "records"}
        or type(summary["version"]) is not int
        or summary["version"] != 1
        or not isinstance(summary["records"], dict)
        or summary["records"].keys() != _GROUPS
    ):
        raise CommandError("Expected a version 1 record summary with all six record groups.")
    for group, fields in summary["records"].items():
        if (
            not isinstance(fields, dict)
            or set(fields) != {"count", "max_id"}
            or type(fields["count"]) is not int
            or not 0 <= fields["count"] <= _MAX_BIGINT
            or not _valid_max_id(group, fields["count"], fields["max_id"])
        ):
            raise CommandError(f"Invalid count or maximum identifier for {group}.")
    return summary["records"]


class Command(BaseCommand):
    help = "Summarize recovery record counts and maximum identifiers, or compare a saved summary."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--compare", metavar="PATH", help="Compare a saved version 1 summary.")

    def handle(self, *args, **options):
        path = options["compare"]
        expected = _load_counts(path) if path is not None else None
        if connection.vendor != "postgresql":
            raise CommandError("The recovery record summary requires PostgreSQL.")
        try:
            with connection.cursor() as cursor:
                cursor.execute(_SUMMARY_SQL)
                records = {group: {"count": n, "max_id": key} for group, n, key in cursor}
        except DatabaseError, ImproperlyConfigured:
            raise CommandError("Could not read the recovery records from PostgreSQL.") from None

        if expected is None:
            return json.dumps({"version": 1, "records": records}, sort_keys=True, indent=2)
        differences = [group for group, fields in records.items() if expected[group] != fields]
        if differences:
            raise CommandError(
                "Recovery record summary differs for " + ", ".join(differences) + "."
            )
        return "All six recovery record groups match the saved summary."
