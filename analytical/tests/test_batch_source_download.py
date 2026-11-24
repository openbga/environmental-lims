"""Batch source links, retained legacy history and staff-only original file downloads."""

from hashlib import sha256

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.http import content_disposition_header

from analytical import services
from analytical.models import ImportSourceFile
from core.models import AuditEvent

from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def batch(lab, analyst):
    return services.create_batch(actor=analyst, method=lab["method"], matrix="water")


def import_event(batch, actor, filename, **details):
    return AuditEvent.objects.create(
        actor=actor,
        action="batch.results_imported",
        object_type=batch._meta.label_lower,
        object_id=str(batch.pk),
        after={"filename": filename, **details},
    )


@pytest.fixture
def source(batch, analyst):
    data = b"\xef\xbb\xbfsample,result\r\nSYN-01,1.2300\r\n"
    filename = "SYN-échantillon.csv"
    digest = sha256(data).hexdigest()
    event = import_event(
        batch, analyst, filename, file_bytes=len(data), file_sha256=digest, source_file_stored=True
    )
    return ImportSourceFile.objects.create(
        batch=batch, event=event, filename=filename, data=data, size=len(data), sha256=digest
    )


def download_url(batch, source):
    return reverse("analytical:batch-import-source-download", args=[batch.pk, source.pk])


def test_reviewer_finds_and_downloads_confirmed_upload_beside_legacy_import(
    client, batch, source, analyst, reviewer, lab
):
    legacy = import_event(batch, analyst, "SYN-older.csv")
    other = services.create_batch(actor=analyst, method=lab["method"], matrix="water")
    client.force_login(reviewer)
    with CaptureQueriesContext(connection) as queries:
        page = client.get(reverse("analytical:batch-detail", args=[batch.pk]))
    assert page.status_code == 200
    assert 'analytical_importsourcefile"."data"' not in " ".join(q["sql"] for q in queries)
    rows = list(page.context["source_page"])
    assert [row["filename"] for row in rows] == [legacy.after["filename"], source.filename]
    assert rows[0]["source_pk"] is None
    assert b"no stored file" in page.content
    url = download_url(batch, source)
    assert url.encode() in page.content

    download = client.get(url)
    assert download.status_code == 200
    assert download.content == bytes(source.data)
    assert download["Content-Disposition"] == content_disposition_header(True, source.filename)
    assert download["Content-Length"] == str(len(download.content))
    assert download["Content-Type"] == "application/octet-stream"
    assert download["Cache-Control"] == "private, no-store"
    assert download["X-Content-Type-Options"] == "nosniff"
    head = client.head(url)
    assert head.status_code == 200 and not head.content
    assert head["Content-Length"] == download["Content-Length"]
    assert client.post(url).status_code == 405
    assert client.get(download_url(other, source)).status_code == 404
    other_page = client.get(reverse("analytical:batch-detail", args=[other.pk]))
    assert b"No result files imported" in other_page.content
    assert url.encode() not in other_page.content


def test_download_requires_an_active_staff_role(client, batch, source, lab):
    url = download_url(batch, source)
    assert client.get(url).status_code == 302
    outsider = make_user("source-file-outsider")
    client.force_login(outsider)
    assert client.get(url).status_code == 403
    portal_user = make_user("source-file-client", "analyst")
    portal_user.client = lab["samples"][0].order.project.client
    portal_user.save(update_fields=["client"])
    client.force_login(portal_user)
    denied = client.get(url)
    assert denied.status_code == 403
    assert "Content-Disposition" not in denied
    assert bytes(source.data) not in denied.content


def test_custom_text_export_extension_downloads_as_text(client, batch, analyst):
    filename = "SYN-run.instrument"
    data = b"sample,result\nSYN-02,4.560\n"
    event = import_event(batch, analyst, filename, file_bytes=len(data), source_file_stored=True)
    source = ImportSourceFile.objects.create(
        batch=batch,
        event=event,
        filename=filename,
        data=data,
        sha256=sha256(data).hexdigest(),
        size=len(data),
    )
    client.force_login(analyst)
    download = client.get(download_url(batch, source))
    assert download.content == data
    assert download["Content-Disposition"] == content_disposition_header(True, filename + ".txt")
