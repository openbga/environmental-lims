"""Bounded current analytical-batch inventories for instrument setup; no state changes."""

import csv
import io
import unicodedata
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db.models import F
from django.shortcuts import get_object_or_404

from accounts.roles import require_role

from .models import Batch, QCSample, Result, ResultRevision

MAX_ROWS = 2_000
COLUMNS = (
    "batch_id",
    "batch_status",
    "method_code",
    "method_version",
    "matrix",
    "equipment_id",
    "row_type",
    "sample_id",
    "client_sample_id",
    "sample_disposition",
    "sample_disposed",
    "requested_test_id",
    "analyte",
    "unit",
    "configured_reporting_limit",
    "result_status",
    "recorded_prepared_aliquot_id",
    "recorded_preparation_batch",
    "recorded_aliquot_label",
    "qc_sample_id",
    "qc_label",
    "qc_type",
    "parent_sample_id",
)
NUMERIC_COLUMNS = frozenset(
    {
        "requested_test_id",
        "configured_reporting_limit",
        "recorded_prepared_aliquot_id",
        "qc_sample_id",
    }
)
FORMULA_PREFIXES = ("=", "+", "-", "@", "＝", "＋", "－", "＠")


@dataclass(frozen=True)
class RunList:
    filename: str
    content: str


def _validate_text(value, *, row, column):
    leading = value.lstrip()
    if (
        any(char in value for char in "\t\r\n")
        or leading.startswith(FORMULA_PREFIXES)
        or any(
            unicodedata.category(char) in {"Cc", "Cf"}
            for char in value[: len(value) - len(leading)]
        )
        or (leading and unicodedata.category(leading[0]) in {"Cc", "Cf"})
    ):
        raise ValidationError(
            f"Run list row {row}, column {column} contains unsafe spreadsheet text. "
            "Correct the source text before downloading; identifiers are never rewritten."
        )


def export_run_list(*, actor, batch_id):
    """Return a fully validated CSV, refusing unsafe text or more than MAX_ROWS rows.

    Membership and configuration are current. A preparation link is included only when the
    latest recorded revision belongs to this batch, never from planned preparation candidates.
    """
    require_role(actor, "analyst")
    batch = get_object_or_404(
        Batch.objects.select_related("method", "equipment"),
        pk=batch_id,
        kind=Batch.Kind.ANALYTICAL,
    )
    results = list(
        Result.objects.filter(batch=batch)
        .select_related(
            "requested_test__sample__storage_record", "requested_test__definition__analyte"
        )
        .order_by(
            "requested_test__sample__lab_id",
            "requested_test__definition__analyte__name",
            "requested_test_id",
        )[: MAX_ROWS + 1]
    )
    if len(results) > MAX_ROWS:
        raise ValidationError(
            f"Run lists are limited to {MAX_ROWS:,} rows. Review batch membership and divide "
            "planned work into smaller batches before downloading."
        )
    qc_samples = list(
        QCSample.objects.filter(batch=batch)
        .select_related("parent_sample")
        .order_by("qc_type", "label", "pk")[: MAX_ROWS - len(results) + 1]
    )
    if len(results) + len(qc_samples) > MAX_ROWS:
        raise ValidationError(
            f"Run lists are limited to {MAX_ROWS:,} rows. Review batch membership and divide "
            "planned work into smaller batches before downloading."
        )
    revisions = {
        revision.result_id: revision
        for revision in ResultRevision.objects.filter(
            result__in=results,
            number=F("result__latest_number"),
            batch=batch,
        ).select_related("prepared_aliquot__batch")
    }
    common = {
        "batch_id": batch.number,
        "batch_status": batch.status,
        "method_code": batch.method.code,
        "method_version": batch.method.version,
        "matrix": batch.matrix,
        "equipment_id": batch.equipment.asset_id if batch.equipment_id else "",
    }
    rows = []
    for result in results:
        test = result.requested_test
        sample, definition = test.sample, test.definition
        storage = getattr(sample, "storage_record", None)
        revision = revisions.get(result.pk)
        aliquot = revision.prepared_aliquot if revision else None
        rows.append(
            {
                **common,
                "row_type": "sample_test",
                "sample_id": sample.lab_id,
                "client_sample_id": sample.client_sample_id,
                "sample_disposition": sample.disposition,
                "sample_disposed": "yes" if storage and storage.disposed_at else "no",
                "requested_test_id": test.pk,
                "analyte": definition.analyte.name,
                "unit": definition.unit,
                "configured_reporting_limit": (
                    format(definition.reporting_limit, "f")
                    if definition.reporting_limit is not None
                    else ""
                ),
                "result_status": result.status,
                "recorded_prepared_aliquot_id": aliquot.pk if aliquot else "",
                "recorded_preparation_batch": aliquot.batch.number if aliquot else "",
                "recorded_aliquot_label": aliquot.label if aliquot else "",
            }
        )
    for sample in qc_samples:
        rows.append(
            {
                **common,
                "row_type": "qc_sample",
                "qc_sample_id": sample.pk,
                "qc_label": sample.label,
                "qc_type": sample.qc_type,
                "parent_sample_id": sample.parent_sample.lab_id if sample.parent_sample_id else "",
            }
        )
    # Validate the complete bounded inventory before constructing a download response.
    for index, row in enumerate(rows, start=2):
        for column, value in row.items():
            if column not in NUMERIC_COLUMNS:
                _validate_text(value, row=index, column=column)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return RunList(filename=f"{batch.number}-run-list.csv", content=output.getvalue())
