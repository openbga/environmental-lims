"""Which pages open in the shared form dialog: presentation, never authorization.

Every form opens in the dialog (docs/ui-standards.md section 7): static/js/form-dialog.js opens a
link to one of these routes in #vr-form-dialog over the page it was clicked on, fetching the page
with the X-VR-Form header; the view, its permission checks, CSRF and validation are the same as
for the full page. Opened by its address, the form opens in the same dialog, and closing it goes
to its record or list. Without JavaScript each stays an ordinary page.

tests/test_form_dialog.py walks every route: one whose view takes a form is listed in
FORM_SURFACES or in NOT_FORM_PAGES with the reason its page is not a form page. A route whose
view accepts only POST is a button or panel form on another page and needs neither.
"""

import re
from dataclasses import dataclass
from functools import cache

from django.urls import URLResolver, get_resolver

# view name -> dialog size: lg for most forms; xl for wide forms, entry grids and review tables.
FORM_SURFACES = {
    "account_change_password": "lg",
    "account_set_password": "lg",
    "mfa_activate_totp": "lg",
    "mfa_deactivate_totp": "lg",
    "mfa_generate_recovery_codes": "lg",
    "receipt:client-create": "lg",
    "receipt:client-edit": "lg",
    "receipt:project-create": "lg",
    "receipt:project-edit": "lg",
    "receipt:contact-create": "lg",
    "receipt:contact-edit": "lg",
    "receipt:order-create": "lg",
    "receipt:order-edit": "lg",
    "receipt:order-add-sample": "xl",
    "receipt:sample-create": "xl",
    "receipt:sample-correct": "xl",
    "receipt:order-correct-reference": "lg",
    "receipt:sample-disposition": "lg",
    "receipt:order-schedule": "lg",
    "receipt:sample-retention": "lg",
    "receipt:sample-disposal-hold": "lg",
    "receipt:sample-dispose": "lg",
    "receipt:subcontract-create": "lg",
    "receipt:subcontract-result-create": "xl",
    "receipt:subcontract-result-review": "lg",
    "receipt:subcontract-cancel": "lg",
    "announcement-create": "lg",
    "announcement-edit": "lg",
    "lab-profile-edit": "lg",
    "lab-logo-edit": "lg",
    "lab-certificate-create": "lg",
    "lab-certificate-edit": "lg",
    "catalog:test-create": "lg",
    "catalog:test-edit": "lg",
    "catalog:method-create": "lg",
    "catalog:method-edit": "lg",
    "catalog:method-new-version": "lg",
    "catalog:analyte-create": "lg",
    "catalog:analyte-edit": "lg",
    "analytical:batch-create": "lg",
    "analytical:calibration-create": "lg",
    "analytical:batch-add-tests": "xl",
    "analytical:batch-entry": "xl",
    "analytical:result-bottles": "xl",
    "analytical:batch-import": "xl",
    "analytical:batch-import-mapped": "xl",
    "dataimport:upload": "xl",
    "analytical:import-profile-create": "xl",
    "analytical:import-profile-edit": "xl",
    "analytical:qc-sample-add": "lg",
    "analytical:qc-measure": "xl",
    "analytical:qc-action": "lg",
    "analytical:config-new": "lg",
    "analytical:config-edit": "lg",
    "analytical:preservation-assess": "lg",
    "analytical:prep-create": "lg",
    "analytical:prep-aliquot-add": "lg",
    "analytical:prep-aliquot-cancel": "lg",
    "analytical:prep-start": "lg",
    "analytical:prep-complete": "lg",
    "analytical:mdl-create": "lg",
    "analytical:mdl-import-blanks": "lg",
    "analytical:mdl-exclude": "lg",
    "analytical:mdl-observation-add": "lg",
    "analytical:mdl-calculate": "lg",
    "laboratory:material-create": "lg",
    "laboratory:material-edit": "lg",
    "laboratory:lot-create": "lg",
    "laboratory:lot-edit": "lg",
    "laboratory:lot-use": "lg",
    "laboratory:lot-receive": "lg",
    "laboratory:lot-adjust": "lg",
    "laboratory:lot-retire": "lg",
    "laboratory:movement-reverse": "lg",
    "laboratory:equipment-create": "lg",
    "laboratory:equipment-edit": "lg",
    "laboratory:equipment-status": "lg",
    "laboratory:event-create": "lg",
    "laboratory:event-correct": "lg",
    "laboratory:training-create": "lg",
    "laboratory:training-renew": "lg",
    "laboratory:training-correct": "lg",
    "laboratory:nc-create": "lg",
    "laboratory:nc-edit": "lg",
    "laboratory:nc-note": "lg",
    "laboratory:nc-step": "lg",
    "commercial:quote-create": "lg",
    "commercial:quote-edit": "xl",
    "commercial:invoice-create": "xl",
    "commercial:invoice-edit": "xl",
    "commercial:invoice-issue": "xl",
    "commercial:invoice-add-line": "lg",
    "commercial:invoice-cancel": "lg",
    "commercial:invoice-void": "lg",
    "commercial:price-list-create": "xl",
    "commercial:price-list-edit": "xl",
    "commercial:submission-receive": "xl",
    "client_commercial:submission-create": "lg",
    "client_commercial:submission-edit": "lg",
    "client_commercial:sample-create": "xl",
    "client_commercial:sample-edit": "xl",
    "reporting:report-create": "xl",
    "reporting:report-edit": "xl",
    "reporting:nysdec-project": "lg",
    "reporting:nysdec-test": "lg",
    "reporting:nysdec-qualifier": "lg",
    "reporting:edd-context": "xl",
}


_SIGN_IN = "a sign-in step: there is no session or workspace page to open it over"
_CONFIRM = (
    "confirms the signed-in person before a two-factor change (sign-in layout); the change it "
    "returns to opens in the dialog"
)
# Routes whose view takes a form but whose page is not a form page, with the reason.
NOT_FORM_PAGES = {
    "account_login": _SIGN_IN,
    "account_signup": _SIGN_IN + "; sign-up is closed",
    "account_request_login_code": _SIGN_IN,
    "account_confirm_login_code": _SIGN_IN,
    "account_reset_password": _SIGN_IN,
    "account_reset_password_from_key": "opened from an emailed link, usually signed out; a "
    "sign-in step",
    "account_confirm_email": "opened from an emailed link, often signed out; a sign-in step",
    "mfa_authenticate": _SIGN_IN,
    "account_reauthenticate": _CONFIRM,
    "mfa_reauthenticate": _CONFIRM,
    "account_email": "the account's email addresses, a state list; Make Primary, Re-send "
    "Verification and Remove act on the address chosen in the list itself, and Add Email opens "
    "in the dialog from the page (form_page local=)",
    "account_logout": "the sign-out confirmation; the user menu's Sign out posts directly",
    "analytical:result-detail": "a record; its correction form opens in the dialog from the "
    "page (form_page local=)",
    "receipt:sample-detail": "a record; Record transfer opens in the dialog from the page "
    "(form_page local=)",
    "analytical:mdl-calculation": "a record; its review decision opens in the dialog from the "
    "page (form_page local=)",
    "reporting:edd-preview": "a record; its only form is the one-button Generate export",
    "analytical:review-batch": "a review queue; the decision applies to the results ticked in "
    "the list itself",
    "analytical:batch-worklist": "a list; Add to batch applies to the tests ticked in the list "
    "itself",
}

FORM_HEADER = "X-VR-Form"


def is_dialog_request(request):
    return request.headers.get(FORM_HEADER) == "1"


@dataclass(frozen=True)
class FormSurface:
    size: str


def surface_for_request(request):
    """Return presentation metadata for an already resolved request."""
    match = getattr(request, "resolver_match", None)
    size = FORM_SURFACES.get(match.view_name) if match is not None else None
    return FormSurface(size) if size else None


@cache
def form_route_patterns():
    """Return JavaScript-compatible path regexes from Django's canonical routes.

    The current application's converters use regex syntax shared by Python and JS.
    Remove Python's named-group spelling and end anchor before exposing the patterns.
    Callers match URL.pathname, without its query string or fragment.
    """
    result = []

    def visit(resolver, prefix="", namespaces=()):
        for route in resolver.url_patterns:
            regex = route.pattern.regex.pattern.removeprefix("^")
            if isinstance(route, URLResolver):
                child_namespaces = namespaces + ((route.namespace,) if route.namespace else ())
                visit(route, prefix + regex, child_namespaces)
                continue
            name = ":".join((*namespaces, route.name)) if route.name else ""
            if name not in FORM_SURFACES:
                continue
            pattern = re.sub(r"\(\?P<[^>]+>", "(?:", prefix + regex)
            pattern = pattern.replace(r"\Z", "$")
            if not pattern.endswith("$"):
                pattern += "$"
            result.append({"pattern": "^/" + pattern, "size": FORM_SURFACES[name]})

    visit(get_resolver())
    return result
