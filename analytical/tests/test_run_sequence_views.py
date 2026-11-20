"""Staff journeys for entering and inspecting recorded instrument injection order."""

import pytest
from django.test import Client as HttpClient
from django.urls import reverse

from analytical import services
from analytical.models import Batch, RunSequence
from receipt.models import RequestedTest

from .conftest import make_user

pytestmark = pytest.mark.django_db

RUN_REFERENCE = "SYN-RUN-01"
INJECTIONS = '1,sample,SYN-OPEN\n2,verification,SYN-CCV\n3,sample, SYN-LABEL, "A" '
FORM_DATA = {
    "reference": RUN_REFERENCE,
    "source_reference": "Synthetic instrument log 01",
    "injections": INJECTIONS,
}


@pytest.fixture
def batch(lab, analyst):
    return services.create_batch(actor=analyst, method=lab["method"], matrix="water")


def sequence_url(action, pk):
    return reverse(f"analytical:run-sequence-{action}", args=[pk])


def create_sequence(client, batch):
    response = client.post(sequence_url("create", batch.pk), FORM_DATA)
    sequence = batch.run_sequences.get()
    assert response.status_code == 302
    assert response.url == sequence_url("detail", sequence.pk)
    return sequence


def test_transcription_correction_and_read_only_history(client, batch, analyst, reviewer):
    client.force_login(analyst)
    batch_url = reverse("analytical:batch-detail", args=[batch.pk])
    create_url = sequence_url("create", batch.pk)
    page = client.get(batch_url)
    assert b"No injection sequences recorded" in page.content
    assert create_url.encode() in page.content
    form = client.get(create_url, headers={"X-VR-Form": "1"})
    assert form.status_code == 200
    assert b"Injections (one per line)" in form.content and b"<textarea" in form.content
    assert b"Reason for correction" not in form.content

    first = create_sequence(client, batch)
    assert first.injections[-1]["label"] == ' SYN-LABEL, "A" '
    first_url = sequence_url("detail", first.pk)
    assert first_url.encode() in client.get(batch_url).content
    correction_url = sequence_url("correct", first.pk)
    correction = client.get(correction_url)
    form = correction.context["form"]
    assert form["injections"].value() == INJECTIONS
    assert form["source_reference"].value() == FORM_DATA["source_reference"]
    assert form["reference"].field.disabled
    assert form["reason"].field.required
    corrected = INJECTIONS + "\n4,verification,SYN-END"
    response = client.post(
        correction_url,
        {
            **FORM_DATA,
            "reference": "FORGED-REFERENCE",
            "injections": corrected,
            "reason": "Transcribed the final recorded verification.",
        },
    )
    current = first.successor
    current_url = sequence_url("detail", current.pk)
    assert response.status_code == 302 and response.url == current_url
    assert current.reference == first.reference
    assert current.version == 2
    assert current.injections[-1] == {"position": 4, "kind": "verification", "label": "SYN-END"}

    client.force_login(reviewer)
    batch_page = client.get(batch_url)
    assert current_url.encode() in batch_page.content
    assert first_url.encode() not in batch_page.content
    assert create_url.encode() not in batch_page.content
    detail = client.get(current_url)
    rows = detail.context["rows"]
    assert rows[0]["before"] is None
    assert rows[0]["after"] == rows[2]["before"] == current.injections[1]
    assert rows[2]["after"] == current.injections[3]
    for shown in (
        b"Verification before",
        b"Verification after",
        b"None recorded",
        b'#2</span> <span class="vr-preserve-space">SYN-CCV',
        b'#4</span> <span class="vr-preserve-space">SYN-END',
        b'<span class="vr-preserve-space"> SYN-LABEL, &quot;A&quot; </span>',
        b"Version history",
        b">Version 1</a>",
        b"Transcribed the final recorded verification.",
    ):
        assert shown in detail.content
    assert f'href="{sequence_url("correct", current.pk)}"'.encode() not in detail.content
    old = client.get(first_url)
    assert b"Superseded" in old.content
    assert current_url.encode() in old.content
    assert old.context["rows"][2]["after"] is None
    assert client.post(current_url, FORM_DATA).status_code == 405


def test_invalid_and_stale_submissions_keep_analyst_input(client, batch, analyst):
    client.force_login(analyst)
    create_url = sequence_url("create", batch.pk)
    for invalid in ("1,sample", "1.0,sample,SYN", "2,sample,SYN", "1,unknown,SYN", "1,sample,"):
        response = client.post(create_url, {**FORM_DATA, "injections": invalid})
        assert response.status_code == 200
        assert response.context["form"].errors["injections"]
        assert response.context["form"]["injections"].value() == invalid
    assert not batch.run_sequences.exists()

    first = create_sequence(client, batch)
    duplicate = client.post(create_url, FORM_DATA)
    assert duplicate.context["form"].errors["reference"]
    correct = sequence_url("correct", first.pk)
    missing_reason = client.post(correct, FORM_DATA)
    assert missing_reason.context["form"].errors["reason"]
    assert client.post(correct, {**FORM_DATA, "reason": "First correction"}).status_code == 302

    stale_input = {**FORM_DATA, "source_reference": "Synthetic second log", "reason": "Stale edit"}
    stale = client.post(correct, stale_input)
    assert stale.status_code == 200
    form = stale.context["form"]
    assert "newer version" in str(form.non_field_errors())
    assert form["source_reference"].value() == stale_input["source_reference"]
    assert form["reason"].value() == stale_input["reason"]
    assert batch.run_sequences.count() == 2
    assert client.get(correct).url == sequence_url("detail", first.pk)


def test_form_accepts_maximum_transcription_with_supplementary_unicode(client, batch, analyst):
    client.force_login(analyst)
    character = "🧪"
    label = character * 120
    data = {
        "reference": character * 120,
        "source_reference": character * 240,
        "injections": "\r\n".join(f"{n},verification,{label}" for n in range(1, 201)),
    }
    create = sequence_url("create", batch.pk)
    form = client.get(create).context["form"]
    for name, value in data.items():
        browser_length = len(value.replace("\r\n", "\n").encode("utf-16-le")) // 2
        widget_capacity = form.fields[name].widget.attrs["maxlength"]
        assert widget_capacity >= browser_length
        assert f'maxlength="{widget_capacity}"' in str(form[name])
    assert client.post(create, data).status_code == 302
    sequence = batch.run_sequences.get()
    assert len(sequence.injections) == 200
    assert all(row["label"] == label for row in sequence.injections)

    correct = sequence_url("correct", sequence.pk)
    form = client.get(correct).context["form"]
    assert 'maxlength="1000"' in str(form["reason"])
    reason = character * 500
    assert client.post(correct, {**data, "reason": reason}).status_code == 302
    corrected = sequence.successor
    assert corrected.reason == reason
    assert corrected.injections == sequence.injections


def test_role_boundary_applies_to_direct_pages_and_forms(client, batch, analyst, reviewer, lab):
    client.force_login(analyst)
    sequence = create_sequence(client, batch)
    create = sequence_url("create", batch.pk)
    correct = sequence_url("correct", sequence.pk)
    detail = sequence_url("detail", sequence.pk)
    portal = make_user("sequence-portal", "analyst", "reviewer")
    portal.client = lab["samples"][0].order.project.client
    portal.save(update_fields=["client"])
    no_role = make_user("sequence-no-role")
    for user, may_read in ((reviewer, True), (portal, False), (no_role, False)):
        client.force_login(user)
        assert client.get(detail).status_code == (200 if may_read else 403)
        for url in (create, correct):
            assert client.get(url, headers={"X-VR-Form": "1"}).status_code == 403
            assert client.post(url, FORM_DATA, headers={"X-VR-Form": "1"}).status_code == 403
    assert HttpClient().get(detail).status_code == 302
    strict_client = HttpClient(enforce_csrf_checks=True)
    strict_client.force_login(analyst)
    assert strict_client.post(create, FORM_DATA).status_code == 403
    assert strict_client.post(correct, {**FORM_DATA, "reason": "CSRF missing"}).status_code == 403
    assert RunSequence.objects.count() == 1


def test_analytical_batch_closure_preserves_access_and_preparation_is_out_of_scope(
    client, batch, analyst, lab
):
    client.force_login(analyst)
    tests = RequestedTest.objects.filter(sample=lab["samples"][0])
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(tests.values_list("pk", flat=True)),
    )
    closed = services.close_batch(actor=analyst, batch_id=batch.pk)
    assert closed.status == Batch.Status.CLOSED
    assert client.get(sequence_url("create", closed.pk)).status_code == 200
    sequence = create_sequence(client, closed)
    correct = sequence_url("correct", sequence.pk)
    assert client.get(correct).status_code == 200
    assert (
        client.post(correct, {**FORM_DATA, "reason": "Clarified after closure"}).status_code == 302
    )
    preparation = services.create_batch(
        actor=analyst, method=lab["method"], matrix="water", kind=Batch.Kind.PREPARATION
    )
    assert client.get(sequence_url("create", preparation.pk)).status_code == 404
    assert client.post(sequence_url("create", preparation.pk), FORM_DATA).status_code == 404
