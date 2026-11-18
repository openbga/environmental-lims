import csv
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from .test_release import release


def template_file():
    return SimpleUploadedFile("client.csv", b"Value,Units,Issue\r\nresult,unit,report_version\r\n")


def test_template_and_example_downloads(world, people, client):
    report = world["report_a"]
    release(report, people["approver"])
    client.force_login(world["user_a"])
    url = reverse("portal:report-csv-template", args=[report.pk, 1])
    example = client.get(reverse("portal:csv-template-example"))
    assert example.status_code == 200
    assert example["Content-Disposition"] == 'attachment; filename="results-template.csv"'
    assert client.get(url).status_code == 200
    for data in ({}, {"template_file": SimpleUploadedFile("bad.csv", b"Only one row")}):
        invalid = client.post(url, data)
        assert invalid.status_code == 200
        assert invalid.context["form"]["template_file"].errors
        assert b"Reselect any files before submitting again." in invalid.content
    response = client.post(url, {"template_file": template_file()})
    assert response["Content-Disposition"] == (
        f'attachment; filename="{report.number}-v1-custom-results.csv"'
    )
    assert response["Cache-Control"] == "private, no-store"
    assert list(csv.reader(StringIO(response.content.decode("utf-8-sig")))) == [
        ["Value", "Units", "Issue"],
        ["0.050", "mg/L", "1"],
    ]


@pytest.mark.parametrize("method", ["get", "post"])
def test_template_only_for_own_issued_version(world, people, client, method):
    report = world["report_a"]
    release(report, people["approver"])
    url = reverse("portal:report-csv-template", args=[report.pk, 1])

    def request(path):
        return (
            client.post(path, {"template_file": template_file()})
            if method == "post"
            else client.get(path)
        )

    assert request(url).status_code == 302
    client.force_login(people["approver"])
    assert request(url).status_code == 403
    client.force_login(world["user_b"])
    assert request(url).status_code == 404
    assert (
        request(reverse("portal:report-csv-template", args=[world["report_b"].pk, 1])).status_code
        == 404
    )
    client.force_login(world["user_a"])
    assert request(reverse("portal:report-csv-template", args=[report.pk, 2])).status_code == 404
