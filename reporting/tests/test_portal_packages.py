from csv import DictReader
from io import BytesIO, StringIO
from zipfile import ZipFile

from django.urls import reverse

from accounts.models import User
from analytical import services as analytical
from receipt.services import update_order_details

from .test_release import correct, release


def package_url(report, number=1):
    return reverse("portal:report-package", args=[report.pk, number])


def package_files(client, report, version, *, include_qc_summary):
    response = client.get(package_url(report, version.number))
    stem = f"{report.number}-v{version.number}"
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"] == f'attachment; filename="{stem}-package.zip"'
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    members = {f"{stem}.pdf", f"{stem}.csv"}
    if include_qc_summary:
        members.add(f"{stem}-qc-summary.txt")
    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == members
        assert archive.read(f"{stem}.pdf") == bytes(version.file.pdf)
        issued_csv = archive.read(f"{stem}.csv")
        csv_url = reverse("portal:report-results-csv", args=[report.pk, version.number])
        assert issued_csv == client.get(csv_url).content
        (row,) = DictReader(StringIO(issued_csv.decode("utf-8-sig"), newline=""))
        return row


def test_package_uses_selected_issue_and_frozen_receipt_preference(world, people, client):
    report = world["report_a"]
    sample = world["sample_a"]
    order = sample.order
    original = release(report, people["approver"])
    assert "include_qc_summary" not in original.file.snapshot["content"]["samples"][0]

    order = update_order_details(
        actor=people["custodian"],
        order_id=order.pk,
        expected_revision=order.revision,
        reason="Synthetic client requested QC summary",
        include_qc_summary=True,
    )
    revision = correct(sample, people, "0.05040")
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    amended = release(report, people["approver2"], reason="Synthetic Lead correction")
    update_order_details(
        actor=people["custodian"],
        order_id=order.pk,
        expected_revision=order.revision,
        reason="Synthetic client preference for future issues",
        include_qc_summary=False,
    )

    client.force_login(world["user_a"])
    detail = client.get(reverse("portal:report-detail", args=[report.pk]))
    assert detail.status_code == 200
    assert detail.content.count(b">Download report package (ZIP)<span") == 2
    assert f'href="{package_url(report, 1)}"'.encode() in detail.content
    assert f'href="{package_url(report, 2)}"'.encode() in detail.content
    old_row = package_files(client, report, original, include_qc_summary=False)
    new_row = package_files(client, report, amended, include_qc_summary=True)
    assert (old_row["report_version"], old_row["result"]) == ("1", "0.050")
    assert (new_row["report_version"], new_row["result"]) == ("2", "0.05040")


def test_package_requires_active_owning_client_and_an_issued_version(world, people, client):
    report, draft = world["report_a"], world["report_b"]
    release(report, people["approver"])
    assert client.get(package_url(report)).status_code == 302

    client.force_login(people["approver"])
    assert client.get(package_url(report)).status_code == 403

    client.force_login(world["user_b"])
    assert client.get(package_url(report)).status_code == 404
    assert client.get(package_url(draft)).status_code == 404

    client.force_login(world["user_a"])
    assert client.get(package_url(report)).status_code == 200
    assert client.get(package_url(report, 2)).status_code == 404
    assert client.post(package_url(report)).status_code == 405
    User.objects.filter(pk=world["user_a"].pk).update(is_active=False)
    assert client.get(package_url(report)).status_code == 302
