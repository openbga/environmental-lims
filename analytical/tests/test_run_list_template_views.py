"""The mapped run-list download uses the same batch and analyst access as the standard export."""

import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from analytical import services
from analytical.models import Batch
from analytical.run_lists import COLUMNS
from analytical.tests.conftest import make_user
from analytical.tests.test_workflow import start
from core.models import AuditEvent

pytestmark = pytest.mark.django_db

DIALOG = {"X-VR-Form": "1"}
DEFAULT_LAYOUT = b'Row,"Label, instrument",Sample\r\nrow_type,qc_label,sample_id\r\n'


def urls(pk):
    return (
        reverse("analytical:batch-run-list-template", args=[pk]),
        reverse("analytical:batch-run-list-template-example", args=[pk]),
    )


def upload(content=DEFAULT_LAYOUT):
    return {"template_file": SimpleUploadedFile("layout.csv", content, content_type="text/csv")}


def assert_csv(response, filename):
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == f'attachment; filename="{filename}"'
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


def test_closed_batch_downloads_reordered_inventory_without_writes(
    config, analyst, admin_user, client
):
    samples = config["samples"]
    batch = start(config, analyst, samples=samples)
    services.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-1")
    Batch.objects.filter(pk=batch.pk).update(
        status=Batch.Status.CLOSED, closed_at=timezone.now(), closed_by=analyst
    )
    client.force_login(admin_user)
    form_url, example_url = urls(batch.pk)
    detail = client.get(reverse("analytical:batch-detail", args=[batch.pk]))
    assert f'href="{form_url}"'.encode() in detail.content
    assert b"Export run list with a template" in detail.content
    audit_count = AuditEvent.objects.count()

    page = client.get(form_url)
    assert page.status_code == 200
    assert b"data-vr-download-form" in page.content and b"multipart/form-data" in page.content
    assert b"<html" in page.content
    assert page.context["batch"].status == Batch.Status.CLOSED
    fragment = client.get(form_url, headers=DIALOG)
    assert fragment.status_code == 200
    assert b"data-vr-form-page" in fragment.content and b"<html" not in fragment.content
    assert f'href="{example_url}"'.encode() in fragment.content

    headings, keys = assert_csv(client.get(example_url), "run-list-template.csv")
    assert tuple(keys) == COLUMNS
    assert len(headings) == len(keys)
    assert client.post(example_url, upload()).status_code == 405
    response = client.post(form_url + "?q=filtered-out&page=999", upload())
    rows = assert_csv(response, f"{batch.number}-run-list.csv")
    assert rows == [
        ["Row", "Label, instrument", "Sample"],
        *[["sample_test", "", sample.lab_id] for sample in samples for _ in range(2)],
        ["qc_sample", "MB-1", ""],
    ]
    tsv = client.post(form_url, {**upload(), "output_format": "tsv"})
    assert tsv.status_code == 200
    assert tsv["Content-Type"] == "text/tab-separated-values; charset=utf-8"
    assert tsv["Content-Disposition"] == f'attachment; filename="{batch.number}-run-list.tsv"'
    assert tsv["Cache-Control"] == "private, no-store"
    assert tsv["X-Content-Type-Options"] == "nosniff"
    assert list(csv.reader(io.StringIO(tsv.content.decode()), delimiter="\t")) == rows
    batch.refresh_from_db()
    assert batch.status == Batch.Status.CLOSED
    assert AuditEvent.objects.count() == audit_count


def test_form_refuses_invalid_file_and_unsafe_inventory_without_a_partial_download(
    config, analyst, client
):
    batch = start(config, analyst)
    client.force_login(analyst)
    form_url, _ = urls(batch.pk)
    for data in ({}, upload(b"Sample\r\nPRIVATE_BAD_KEY\r\n")):
        invalid = client.post(form_url, data)
        assert invalid.status_code == 200 and "Content-Disposition" not in invalid
        assert invalid.context["form"]["template_file"].errors
        assert b"Reselect any files before submitting again." in invalid.content
        assert b"PRIVATE_BAD_KEY" not in invalid.content

    invalid = client.post(form_url, {**upload(), "output_format": "xlsx"})
    assert invalid.status_code == 200 and "Content-Disposition" not in invalid
    assert invalid.context["form"]["output_format"].errors

    services.add_qc_sample(
        actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="=unsafe"
    )
    invalid = client.post(form_url, {**upload(b"Row\r\nrow_type\r\n"), "output_format": "tsv"})
    assert invalid.status_code == 200 and "Content-Disposition" not in invalid
    assert b"Run list not exported." in invalid.content
    assert b"column qc_label contains unsafe spreadsheet text" in invalid.content
    assert b"Reselect the template file before submitting again." in invalid.content
    assert b"=unsafe" not in invalid.content


def test_template_routes_require_login_analyst_access_and_an_analytical_batch(
    config, analyst, reviewer, client
):
    batch = start(config, analyst)
    form_url, example_url = urls(batch.pk)

    def statuses():
        return (
            client.get(form_url).status_code,
            client.post(form_url, upload()).status_code,
            client.get(example_url).status_code,
        )

    assert statuses() == (302, 302, 302)
    client.force_login(reviewer)
    assert statuses() == (403, 403, 403)
    detail = client.get(reverse("analytical:batch-detail", args=[batch.pk]))
    assert f'href="{form_url}"'.encode() not in detail.content

    portal_user = make_user("run-list-template-client", "analyst")
    portal_user.client = config["samples"][0].order.project.client
    portal_user.save()
    client.force_login(portal_user)
    assert statuses() == (403, 403, 403)

    client.force_login(analyst)
    prep = Batch.objects.create(
        created_by=analyst, method=config["method"], matrix="water", kind=Batch.Kind.PREPARATION
    )
    for pk in (prep.pk, prep.pk + 1):
        form_url, example_url = urls(pk)
        assert statuses() == (404, 404, 404)

    strict_client = Client(enforce_csrf_checks=True)
    strict_client.force_login(analyst)
    assert strict_client.post(urls(batch.pk)[0], upload()).status_code == 403
