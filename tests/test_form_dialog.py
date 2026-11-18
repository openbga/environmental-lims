"""Dialog transport retains normal form validation, access checks and redirect semantics."""

import inspect
import json
import re
from types import SimpleNamespace

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    format_hotp_value,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.contrib.messages import get_messages
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from django.urls import URLResolver, get_resolver, reverse
from django.views.generic.edit import ProcessFormView

from core.form_dialog import FormDialogMiddleware
from core.form_surfaces import (
    FORM_SURFACES,
    NOT_FORM_PAGES,
    form_route_patterns,
    surface_for_request,
)
from receipt.models import Client

DIALOG = {"X-VR-Form": "1"}
PASSWORD = "test-password-729!"  # tests/conftest.py staff()
NEW_PASSWORD = "Synthetic-dialog-change-2026!"


@pytest.mark.parametrize("enhanced", [False, True])
def test_redirect_to_next_form_keeps_native_or_dialog_transport(enhanced):
    destination = reverse("receipt:client-create") + "?next=another"
    request = RequestFactory().post("/", headers={"X-VR-Form": "1"} if enhanced else {})
    response = FormDialogMiddleware(lambda request: HttpResponseRedirect(destination))(request)
    assert response.status_code == (204 if enhanced else 302)
    if enhanced:
        location = json.loads(response["HX-Location"])
        assert location == {
            "path": destination,
            "target": "#vr-form-content",
            "push": "false",
            "headers": {"X-VR-Form": "1"},
        }
        assert "Location" not in response
    else:
        assert response["Location"] == destination
    assert "X-VR-Form" in response["Vary"]


@pytest.mark.django_db
def test_form_fragment_retains_validation_and_success_message(client, staff):
    client.force_login(staff("dialog_pm", "project_manager"))
    url = reverse("receipt:client-create")
    ordinary = client.get(url)
    assert b"<!doctype html>" in ordinary.content
    assert b"data-vr-form-page" in ordinary.content
    fragment = client.get(url, headers={"X-VR-Form": "1"})
    assert fragment.status_code == 200
    assert b"<!doctype html>" not in fragment.content
    assert b"data-vr-form-page" in fragment.content
    assert b"csrfmiddlewaretoken" in fragment.content
    invalid = client.post(
        url, {"name": "", "contact_name": "Synthetic contact"}, headers={"X-VR-Form": "1"}
    )
    assert invalid.status_code in (200, 422)
    assert b"Synthetic contact" in invalid.content
    assert b'aria-invalid="true"' in invalid.content
    assert not Client.objects.filter(contact_name="Synthetic contact").exists()
    saved = client.post(url, {"name": "Synthetic dialog client"}, headers={"X-VR-Form": "1"})
    record = Client.objects.get(name="Synthetic dialog client")
    assert saved.status_code == 204
    assert saved["HX-Redirect"] == reverse("receipt:client-detail", args=[record.pk])
    destination = client.get(saved["HX-Redirect"])
    assert "Create client completed." in [
        str(message) for message in get_messages(destination.wsgi_request)
    ]


@pytest.mark.django_db
def test_dialog_header_does_not_bypass_access_or_csrf(client, staff):
    url = reverse("receipt:client-create")
    response = client.get(url, headers={"X-VR-Form": "1"})
    assert response.status_code == 204
    assert response["HX-Redirect"].startswith(reverse("account_login"))
    user = staff("dialog_analyst", "analyst")
    client.force_login(user)
    denied = client.get(url)
    assert denied.status_code == 403
    assert b"data-vr-form-page" not in denied.content
    assert client.get(url, headers={"X-VR-Form": "1"}).status_code == 403
    assert client.post(url, {"name": "Forbidden"}, headers={"X-VR-Form": "1"}).status_code == 403
    from django.test import Client as HttpClient

    strict_client = HttpClient(enforce_csrf_checks=True)
    strict_client.force_login(staff("dialog_csrf", "project_manager"))
    assert (
        strict_client.post(url, {"name": "No token"}, headers={"X-VR-Form": "1"}).status_code == 403
    )


def routes(resolver=None, namespaces=()):
    """(view name, view) for every named route outside the admin."""
    for route in (resolver or get_resolver()).url_patterns:
        if isinstance(route, URLResolver):
            if route.namespace != "admin":
                spaces = namespaces + ((route.namespace,) if route.namespace else ())
                yield from routes(route, spaces)
        elif route.name:
            yield ":".join((*namespaces, route.name)), route.callback


POSTED = re.compile(r"request\.POST|request\.method\s*==\s*[\"']POST")
POST_ONLY = re.compile(r"@require_POST\b|@require_http_methods\(\s*\[\s*[\"']POST[\"']\s*\]\s*\)")


def source(function):
    try:
        return inspect.getsource(inspect.unwrap(function))
    except OSError, TypeError:
        return ""


def takes_form(view):
    """The view shows a page that submits a form: a form view class, or a function that reads a
    POST (itself or through a helper of its module) and also answers GET. A POST-only view is a
    button or panel form on another page."""
    view_class = getattr(view, "view_class", None)
    if view_class is not None:
        return issubclass(view_class, ProcessFormView) or hasattr(view_class, "post")
    text = source(view)
    if POST_ONLY.search(text):
        return False
    if POSTED.search(text):
        return True
    module = inspect.getmodule(inspect.unwrap(view))
    helpers = (getattr(module, name, None) for name in re.findall(r"\b(\w+)\(\s*request\b", text))
    return any(inspect.isfunction(h) and POSTED.search(source(h)) for h in helpers)


def test_every_form_opens_in_the_dialog():
    """Every route whose view takes a form is a dialog form, or says why its page is not one."""
    named = dict(routes())
    found = {name for name, view in named.items() if takes_form(view)}
    unexplained = found - set(FORM_SURFACES) - set(NOT_FORM_PAGES)
    assert not unexplained, f"add to FORM_SURFACES (core/form_surfaces.py): {sorted(unexplained)}"
    assert not set(FORM_SURFACES) & set(NOT_FORM_PAGES)
    assert set(NOT_FORM_PAGES) <= found, "an exception no longer takes a form"
    assert set(FORM_SURFACES) | set(NOT_FORM_PAGES) <= set(named), "a listed route is gone"
    assert all(reason.strip() for reason in NOT_FORM_PAGES.values())


def test_the_route_walk_finds_form_pages_and_skips_button_actions():
    named = dict(routes())
    assert takes_form(named["receipt:client-create"])  # through its module's helper
    assert takes_form(named["account_change_password"])  # a form view class
    assert takes_form(named["analytical:result-detail"])  # a record with a local form
    assert not takes_form(named["commercial:quote-issue"])  # POST only: a header button
    assert not takes_form(named["receipt:order-list"])


def test_pages_that_are_not_forms_never_open_in_the_dialog():
    for name in NOT_FORM_PAGES:
        match = SimpleNamespace(resolver_match=SimpleNamespace(view_name=name))
        assert surface_for_request(match) is None
    patterns = [entry["pattern"] for entry in form_route_patterns()]
    for path in (reverse("mfa_index"), reverse("account_login"), reverse("account_password")):
        assert not any(re.fullmatch(pattern, path) for pattern in patterns), path


@pytest.mark.django_db
def test_dialog_fragment_has_no_shell_and_no_history(client, staff):
    client.force_login(staff("dialog_history_pm", "project_manager"))
    record = Client.objects.create(name="Synthetic history client")
    url = reverse("receipt:client-edit", args=[record.pk])
    assert b"<!doctype html>" in client.get(url).content
    fragment = client.get(url, headers=DIALOG).content
    assert b"data-vr-form-page" in fragment
    assert b"<!doctype html>" not in fragment and b"vr-sidenav" not in fragment
    assert b"vr-history" not in fragment and b"vr-timeline" not in fragment


@pytest.mark.django_db
def test_password_changes_in_the_dialog_and_returns_to_its_state_page(client, staff):
    user = staff("dialog_password_pm", "project_manager")
    client.force_login(user)
    state = client.get(reverse("account_password"))
    assert state.status_code == 200 and b"<!doctype html>" in state.content
    assert f'href="{reverse("account_change_password")}"'.encode() in state.content
    opened = client.get(reverse("account_change_password"), headers=DIALOG)
    assert opened.status_code == 200 and b"<!doctype html>" not in opened.content
    assert b"data-vr-form-page" in opened.content
    assert f'href="{reverse("account_password")}">Cancel'.encode() in opened.content
    wrong = client.post(
        reverse("account_change_password"),
        {"oldpassword": "not-the-password", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        headers=DIALOG,
    )
    assert wrong.status_code == 200 and b'aria-invalid="true"' in wrong.content
    changed = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        headers=DIALOG,
    )
    assert changed.status_code == 204
    assert changed["HX-Redirect"] == reverse("account_password")
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    page = client.get(changed["HX-Redirect"])
    assert any("Password successfully changed" in str(m) for m in get_messages(page.wsgi_request))


@pytest.mark.django_db
def test_two_factor_change_in_the_dialog_still_asks_to_confirm_access(client, staff):
    """allauth's reauthentication check runs unchanged: the dialog hands the page to it."""
    user = staff("dialog_mfa_pm", "project_manager")
    client.force_login(user)
    overview = client.get(reverse("mfa_index"))
    assert overview.status_code == 200 and b"<!doctype html>" in overview.content
    asked = client.get(reverse("mfa_activate_totp"), headers=DIALOG)
    assert asked.status_code == 204
    assert asked["HX-Redirect"].startswith(reverse("account_reauthenticate"))
    confirmed = client.post(asked["HX-Redirect"], {"password": PASSWORD})
    assert confirmed.status_code == 302
    opened = client.get(reverse("mfa_activate_totp"), headers=DIALOG)
    assert opened.status_code == 200 and b"<!doctype html>" not in opened.content
    assert f'href="{reverse("mfa_index")}">Cancel'.encode() in opened.content
    secret = opened.context["form"].secret
    refused = client.post(reverse("mfa_activate_totp"), {"code": "000000"}, headers=DIALOG)
    assert refused.status_code == 200 and b'aria-invalid="true"' in refused.content
    counters = list(yield_hotp_counters_from_time())
    code = format_hotp_value(hotp_value(secret, counters[len(counters) // 2]))
    activated = client.post(reverse("mfa_activate_totp"), {"code": code}, headers=DIALOG)
    assert activated.status_code == 204
    assert activated["HX-Redirect"] in (reverse("mfa_index"), reverse("mfa_view_recovery_codes"))
    assert Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()


@pytest.mark.django_db
def test_set_password_opens_in_the_dialog_for_an_account_without_one(client, staff):
    user = staff("dialog_no_password", "project_manager")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    client.force_login(user)
    state = client.get(reverse("account_password"))
    assert b"Not set" in state.content
    assert f'href="{reverse("account_set_password")}"'.encode() in state.content
    opened = client.get(reverse("account_set_password"), headers=DIALOG)
    assert opened.status_code == 200 and b"<!doctype html>" not in opened.content
    saved = client.post(
        reverse("account_set_password"),
        {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        headers=DIALOG,
    )
    assert saved.status_code == 204 and saved["HX-Redirect"] == reverse("account_password")
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
