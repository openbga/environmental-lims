"""Download an issued report package from its stored snapshot and PDF."""

import re
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from analytical.models import QCType

from .content import OTHER_SAMPLE, qc_test_key
from .results_csv import export_results_csv

QC_TYPE_LABELS = dict(QCType.choices)


def export_report_package(snapshot, pdf) -> bytes:
    """Bundle the issued PDF, standard result CSV and any requested QC summary."""
    report = snapshot["content"]["report"]["number"]
    version = snapshot["release"]["version"]
    # Report.number is issued from its numeric primary key; archive paths use only this identity.
    if not re.fullmatch(r"RPT-[0-9]+", report) or type(version) is not int or version < 1:
        raise ValueError("Report package requires a valid issued report number and version.")
    stem = f"{report}-v{version}"

    members = [
        (f"{stem}.pdf", bytes(pdf)),
        (f"{stem}.csv", export_results_csv(snapshot).encode("utf-8")),
    ]
    qc = _qc_summary(snapshot)
    if qc:
        members.append((f"{stem}-qc-summary.txt", qc.encode("utf-8")))

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, data in members:
            member = ZipInfo(name)  # A fixed DOS timestamp, independent of download time.
            member.create_system = 3  # Keep the same file metadata on Unix and Windows.
            archive.writestr(member, data, compress_type=ZIP_DEFLATED)
    return output.getvalue()


def _result_qualifiers(result, meanings):
    if result.get("source") == "external":
        return result.get("external_qualifiers") or "None reported"
    return (
        ", ".join(
            f"{code} ({meanings[code]})" if meanings.get(code) else code
            for code in result.get("qualifiers", [])
        )
        or "None"
    )


def _qc_summary(snapshot):
    content = snapshot["content"]
    requested = [s for s in content["samples"] if s.get("include_qc_summary") is True]
    if not requested:
        return ""

    report = content["report"]["number"]
    version = snapshot["release"]["version"]
    lines = ["Quality control summary", f"Report {report} · Version {version}"]
    if content.get("synthetic_notice"):
        lines.append("Synthetic demonstration data.")
    lines.append(f"Requested samples · {', '.join(s['lab_id'] for s in requested)}")

    meanings = {q["code"]: q["description"] for q in content.get("qualifiers", [])}
    groups = {}
    for batch in content.get("batches", []):
        kind = batch.get("kind", "analytical")
        for analyte, qc in batch["qc"].items():
            groups[(kind, batch["number"], qc_test_key(qc, analyte))] = (batch, analyte, qc)
    selected = {}
    without_qc = []
    for sample in requested:
        lab_id = sample["lab_id"]
        if not sample["results"]:
            without_qc.append(f"{lab_id} · No results recorded.")
        for result in sample["results"]:
            analyte = result["analyte"]
            qualifiers = _result_qualifiers(result, meanings)
            label = f"{lab_id} · {analyte} · Qualifiers {qualifiers}"
            if result.get("source") == "external":
                without_qc.append(
                    f"{label} · Subcontracted result. "
                    "QC was not evaluated by the issuing laboratory."
                )
                continue
            test = qc_test_key(result, analyte)
            preparation = result.get("preparation") or {}
            batch_numbers = [
                ("analytical", result.get("batch")),
                ("preparation", preparation.get("batch")),
            ]
            for kind, number in batch_numbers:
                if number:
                    selected.setdefault((kind, number, test), []).append(label)
                elif kind == "analytical":
                    without_qc.append(f"{label} · No analytical batch recorded.")

    for (kind, number, test), samples in selected.items():
        batch, analyte, qc = groups.get((kind, number, test), ({}, "", None))
        identity = f"{kind.capitalize()} batch {number}"
        if batch.get("method"):
            identity = f"{identity} · {batch['method']}"
        lines.extend(("", identity))
        if analyte:
            lines.append(f"QC analyte · {analyte}")
        lines.extend(samples)
        if qc is None:
            lines.append("No QC evidence recorded for this test.")
        else:
            lines.extend(_qc_evidence(qc))
    if without_qc:
        lines.extend(("", *without_qc))
    return "\n".join(lines) + "\n"


def _words(value):
    return value.replace("_", " ")


def _qc_evidence(qc):
    """Print the scoped observations, evaluations and dispositions stored at release."""
    if not qc.get("measurements"):
        yield "No QC measurements recorded for this test."
    for measurement in qc.get("measurements", []):
        qc_type = measurement["qc_type"]
        label = QC_TYPE_LABELS.get(qc_type, _words(qc_type))
        withheld = measurement.get("withheld")
        evaluation = measurement.get("evaluation") or {}
        status = _words(evaluation.get("status") or "not recorded")
        if withheld:
            yield f"QC sample · {OTHER_SAMPLE} · {label} · Observed value withheld"
        else:
            observed = measurement.get("observed_text")
            value = (
                f"{observed} {measurement.get('unit', '')}".rstrip()
                if observed is not None
                else "not recorded"
            )
            yield f"QC sample · {measurement['qc_sample']} · {label} · Observed {value}"
            if measurement.get("analysis_batch"):
                yield f"  Analytical run · {measurement['analysis_batch']}"
        yield f"  Recorded evaluation · {status.capitalize()}"
        for measure in evaluation.get("measures", []):
            if withheld:
                yield f"  {measure['measure'].capitalize()} · {_words(measure['status'])}"
                for key in ("lower", "upper"):
                    if measure.get(key) is not None:
                        yield f"    {key.capitalize()} limit · {measure[key]}"
            elif measure.get("detail"):
                yield f"  {measure['detail']}"
    for issue in qc.get("issues", []):
        yield f"QC issue · {issue['message']}"
        if issue.get("disposition"):
            yield f"  Disposition · {_words(issue['disposition'])}"
        if issue.get("note_withheld"):
            yield "  Disposition note withheld"
        elif issue.get("note"):
            yield f"  Disposition note · {issue['note']}"
