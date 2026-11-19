"""Inspect a line fit in Chromium without changing the calibration that supplies its points."""

from datetime import timedelta

import pytest
from helpers import field, pick
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_fit_stays_separate_from_recorded_calibration(sign_in_as):
    from django.utils import timezone

    from accounts.models import User
    from analytical.calibration import record_calibration
    from analytical.services import create_batch
    from catalog.models import TestDefinition

    analyst = User.objects.get(username="demo_analyst")
    definition = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    )
    batch = create_batch(actor=analyst, method=definition.method, matrix=definition.matrix)
    source = "Synthetic instrument export 100"
    model = "Instrument fit y = 2.8000 x - 3.1000"
    points = [
        {"standard": "Blank", "concentration": "0.0000", "response": "-1.0000"},
        {"standard": "Low", "concentration": "1.0000", "response": "3.0000"},
        {"standard": "High", "concentration": "2.0000", "response": "4.0000"},
    ]
    curve = record_calibration(
        actor=analyst,
        batch_id=batch.pk,
        definition_id=definition.pk,
        reference="SYNTHETIC-FIT-WORKSHEET",
        calibrated_at=timezone.now() - timedelta(hours=1),
        model_text=model,
        response_unit="counts",
        source_reference=source,
        points=points,
    )

    page = sign_in_as("reviewer")
    page.set_viewport_size({"width": 390, "height": 844})
    path = f"/analytical/calibrations/{curve.pk}/"
    assert page.goto(path).ok
    form = page.get_by_role("region", name="Linear fit worksheet", exact=True)
    table = page.get_by_role("table", name="Standard points")
    residual = table.get_by_role("columnheader", name="Residual (counts)", exact=True)
    button = form.get_by_role("button", name="Calculate fit", exact=True)
    weighting = field(form, "Weighting")
    expect(residual).to_have_count(0)

    weighting.press("Tab")
    expect(button).to_be_focused()
    button.press("Enter")
    expect(form).to_contain_text("Choose a weighting to calculate the fit.")
    expect(residual).to_have_count(0)

    pick(weighting, "equal")
    with page.expect_request(
        lambda request: request.is_navigation_request() and f"{path}?" in request.url
    ) as load:
        button.press("Enter")
    assert load.value.method == "GET"
    expect(residual).to_be_visible()
    expect(table.get_by_role("row", name="Blank 0.0000 -1.0000 -0.5 -0.5")).to_be_visible()
    expect(table.get_by_role("row", name="Low 1.0000 3.0000 2 1")).to_be_visible()
    expect(table.get_by_role("row", name="High 2.0000 4.0000 4.5 -0.5")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    page.get_by_role("button", name="Help", exact=True).press("Enter")
    drawer = page.locator("#vr-documentation")
    expect(drawer).to_contain_text("Residual")
    drawer.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(drawer).to_be_hidden()

    pick(weighting, "inverse")
    button.press("Enter")
    expect(form).to_contain_text("Fit unavailable")
    expect(form).to_contain_text(
        "Inverse weighting requires every concentration to be greater than zero."
    )
    expect(residual).to_have_count(0)
    expect(table.get_by_role("row", name="Blank 0.0000 -1.0000")).to_be_visible()

    assert page.goto(path).ok
    expect(residual).to_have_count(0)
    facts = page.get_by_role("region", name="Calibration facts")
    expect(facts).to_contain_text(model)
    expect(facts).to_contain_text(source)
    curve.refresh_from_db()
    assert (curve.model_text, curve.source_reference, curve.points) == (model, source, points)
    assert not page.console_errors
