"""The controls the first journey rests on, each checked on its own.

Authorization is checked through HTTPS requests to the real URLs, so a refusal comes from the
server, not from a hidden button. The history checks write to history tables directly on
purpose, to show that the database itself refuses to update or delete them.
"""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from accounts.models import User
from accounts.roles import can_authorize_release
from analytical import services as bench
from analytical import worksheets
from analytical.holding import duration_label
from analytical.models import (
    CalculationRule,
    HoldingTimeRule,
    Result,
    ResultRevision,
    WorksheetReading,
)
from catalog.models import Method
from core.models import AuditEvent
from receipt import services as receipt
from receipt.models import Client, CustodyEvent, Sample
from reporting.content import assemble
from reporting.models import Report, ReportFile, ReportVersion
from reporting.services import create_report, release_report

from .journey import (
    NUTRIENTS,
    OWN_RULE_ANALYTE,
    VERSION,
    account,
    create_client_user,
    download,
    enter,
    receive,
    sha256,
    signed_in,
    tests_for,
)
from .runner import csrf_refusal, role_refusal, step
from .scratch import sqlstate

# Each case: (who is refused, method, path, why; who may, the status that shows they may).
# A refusal only counts when the same request succeeds for the right person.
REFUSALS = [
    (None, "get", "/samples/", "anonymous: sent to sign-in", "custodian", 200),
    (None, "get", "/reports/{report}/v1.pdf", "anonymous: no download", "manager", 200),
    ("custodian", "get", "/analytical/batches/new/", "custodian builds no batches", "analyst", 200),
    ("manager", "get", "/analytical/batches/new/", "manager builds no batches", "analyst", 200),
    ("reviewer", "get", "/orders/new/", "reviewer opens no custody", "custodian", 200),
    ("analyst", "get", "/clients/new/", "analyst creates no clients", "manager", 200),
    # The approver passes authorization and is then refused for the dummy content hash (409).
    ("analyst", "post", "/reports/{report}/release/", "analyst cannot release", "approver", 409),
    ("custodian", "get", "/admin/accounts/user/", "a role opens no account admin", "admin", 200),
    ("client_a", "get", "/samples/", "client: no staff sample list", "custodian", 200),
    ("client_a", "get", "/reports/{report}/v1.pdf", "client: no staff download", "manager", 200),
]


def request(run, who, method, path):
    browser = signed_in(run, who) if who else run.browser()
    if method == "get":
        response = browser.get(path)
    else:
        response = browser.post(
            path, {"fingerprint": "0" * 64, "confirm": "on", "reason": "Synthetic attempt"}
        )
    location = response.get("Location", "")
    if who and response.status_code == 302 and location.startswith("/admin/login/"):
        response = browser.get(location)  # the admin's sign-in hand-off decides
    return response


@step(
    "C01",
    "Role refusals on the server",
    requirement="Authorize every server-side read and mutation: each role reaches only its own "
    "work, whatever the interface shows (docs/architecture.md, application boundaries; "
    "outcomes 9 and 12).",
    expected="Each request outside the account's role is refused by the server: anonymous "
    "visitors are sent to sign-in (302), signed-in accounts get the authorization 403 (not a "
    "CSRF failure); the same request succeeds for an account whose role permits it.",
    depends=("O12",),
)
def role_refusals(run):
    report = run["report_a"].pk
    for who, method, template, why, allowed, status in REFUSALS:
        path = template.format(report=report)
        response = request(run, who, method, path)
        if who is None:
            refused = response.status_code == 302 and response["Location"].startswith(
                "/accounts/login/"
            )
        else:
            refused = role_refusal(response)
        permitted = request(run, allowed, method, path)
        run.check(
            refused and permitted.status_code == status,
            f"{who or 'anonymous'} {method.upper()} {path}: HTTP {response.status_code} ({why}); "
            f"acceptance_{allowed}: HTTP {permitted.status_code}",
            f"{'302 to sign-in' if who is None else 'authorization 403'}, and {status} for "
            f"acceptance_{allowed}",
        )
    run.check(
        ReportVersion.objects.filter(report_id=report).count() == 2,
        "No request in this step released anything",
    )


@step(
    "C02",
    "Client isolation by direct address",
    requirement="A client account sees only its own records, even when it asks for another "
    "client's report by its address (outcome 12).",
    expected="Each client account opens its own report page and PDF (HTTP 200, stored bytes) "
    "and gets 404 for the other client's report page and PDF at the same addresses; its report "
    "list holds only its own report.",
    depends=("O12",),
)
def client_isolation(run):
    approver = account(run, "approver")
    report_b = create_report(
        actor=account(run, "manager"),
        project=run["project_b"],
        title="Reservoir acceptance report (synthetic)",
        samples=[run["sample_b1"]],
    )
    release_report(
        actor=approver,
        report_id=report_b.pk,
        expected_fingerprint=assemble(
            Report.objects.get(pk=report_b.pk), actor=approver
        ).fingerprint,
    )
    create_client_user(run, "client_b", run["project_b"].client)
    report_a = run["report_a"]
    for who, own, other in (("client_a", report_a, report_b), ("client_b", report_b, report_a)):
        browser = signed_in(run, who)
        stored = bytes(ReportFile.objects.get(version__report=own, version__number=1).pdf)
        page = browser.get(f"/portal/reports/{own.pk}/")
        pdf = browser.get(f"/portal/reports/{own.pk}/v1.pdf")
        run.check(
            page.status_code == 200 and pdf.status_code == 200 and pdf.content == stored,
            f"acceptance_{who} opens its own {own.number}: page HTTP {page.status_code}, "
            f"v1.pdf HTTP {pdf.status_code} with the stored bytes",
            "200 and 200",
        )
        for path in (f"/portal/reports/{other.pk}/", f"/portal/reports/{other.pk}/v1.pdf"):
            response = browser.get(path)
            run.check(
                response.status_code == 404,
                f"acceptance_{who} GET {path} ({other.number}, the other client's): "
                f"HTTP {response.status_code}",
                "404",
            )
        listing = browser.get("/portal/reports/").content.decode("utf-8", "replace")
        run.check(
            own.number in listing and other.number not in listing,
            f"acceptance_{who} report list shows {own.number}, not {other.number}",
        )


@step(
    "C03",
    "Nobody reviews their own results",
    requirement="A qualified person other than the analyst performs technical review; nobody "
    "reviews their own results, even when holding both roles (outcome 8).",
    expected="An account with both analyst and reviewer roles enters and submits a result. Its "
    "own approval and return are refused by the service, and through the review page each is "
    "answered with the page's own refusal (redirect back, warning shown) while the result stays "
    "submitted. The independent reviewer's return through the same page succeeds.",
    depends=("O03",),
)
def independent_review(run):
    dual = account(run, "analyst_reviewer")
    sample = run["sample_a3"]
    batch = bench.create_batch(
        actor=dual,
        method=Method.objects.get(code=NUTRIENTS, version=VERSION),
        matrix="water",
        prepared_at=timezone.now() - timedelta(minutes=40),
    )
    bench.add_tests_to_batch(
        actor=dual,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    revision = enter(run, sample, "Nitrate as N", "1.20", rl="0.05", who="analyst_reviewer")
    bench.submit_result(actor=dual, revision_id=revision.pk)
    for decide in (
        lambda: bench.approve_result(actor=dual, revision_id=revision.pk),
        lambda: bench.return_result(actor=dual, revision_id=revision.pk, reason="Own work"),
    ):
        try:
            decide()
            refusal = None
        except PermissionDenied as exc:
            refusal = str(exc)
        run.check(
            refusal is not None and "you cannot review it" in refusal,
            f"Service refused acceptance_analyst_reviewer: '{refusal}'",
            "the independence refusal",
        )
    page = f"/analytical/review/batches/{batch.pk}/"
    browser = signed_in(run, "analyst_reviewer")
    for action, note in (("approve", ""), ("return", "Synthetic: own work, returned")):
        response = browser.post(page, {"action": action, "note": note, "revisions": [revision.pk]})
        shown = browser.get(page).content.decode("utf-8", "replace")
        warned = "someone else must review work you entered or submitted" in shown
        back = response.get("Location") == page
        revision.refresh_from_db()
        run.check(
            response.status_code == 302 and back and warned and revision.status == "submitted",
            f"{action.capitalize()} of own work through the review page: HTTP "
            f"{response.status_code}, {'back to the page' if back else 'not back to the page'}, "
            f"independence warning {'shown' if warned else 'not shown'}; result "
            f"'{revision.status}'",
            "the page's own refusal and a submitted result",
        )
    response = signed_in(run, "reviewer").post(
        page,
        {"action": "return", "note": "Synthetic: independent return.", "revisions": [revision.pk]},
    )
    revision.refresh_from_db()
    run.check(
        response.status_code == 302 and revision.status == "returned",
        f"The same return by acceptance_reviewer: HTTP {response.status_code}, result "
        f"'{revision.status}'",
        "returned",
    )


@step(
    "C04",
    "Release needs explicit permission",
    requirement="Release is authorized only by an account holding the explicit release "
    "permission; an administrator or reviewer role alone does not grant it (outcome 9).",
    expected="The administrator and the reviewer are refused by the release service and by the "
    "release URL (authorization 403, not a CSRF failure); the approver's identical request "
    "passes authorization and is refused only for its stale content hash (409); only the "
    "approver group carries the permission.",
    depends=("O09",),
)
def release_permission(run):
    report = Report.objects.get(pk=run["report_a"].pk)
    for who in ("admin", "reviewer"):
        user = account(run, who)
        try:
            release_report(actor=user, report_id=report.pk, expected_fingerprint="0" * 64)
            refusal = None
        except PermissionDenied as exc:
            refusal = str(exc)
        run.check(
            refusal is not None and "release permission" in refusal,
            f"Release service refused acceptance_{who}: '{refusal}'",
            "the release-permission refusal",
        )
    attempt = {"fingerprint": "0" * 64, "confirm": "on", "reason": "Synthetic attempt"}
    path = f"/reports/{report.pk}/release/"
    for who in ("admin", "reviewer"):
        response = signed_in(run, who).post(path, attempt)
        run.check(
            role_refusal(response),
            f"Release URL for acceptance_{who}: HTTP {response.status_code}",
            "authorization 403",
        )
    response = signed_in(run, "approver").post(path, attempt)
    run.check(
        response.status_code == 409,
        f"The same request by acceptance_approver: HTTP {response.status_code} (authorized; "
        "refused for the stale content hash)",
        "409",
    )
    accounts = User.objects.filter(username__startswith="acceptance_", client__isnull=True)
    holders = sorted(u.username for u in accounts if can_authorize_release(u))
    run.check(
        holders == ["acceptance_approver"],
        f"Staff accounts holding release permission: {holders}; the administrator, a Django "
        "superuser, holds none",
        "['acceptance_approver']",
    )


@step(
    "C05",
    "Holding time computed from the rule",
    requirement="Expiry is computed from the configured rule (start event, end event, duration), "
    "never typed; a result past its limit is flagged and needs a holding-time qualifier "
    "(outcome 4; 40 CFR 136.3 Table II footnote 4 as cited in docs/product.md).",
    expected="Changing the rule duration moves the deadline shown for an unanalysed sample; a "
    "result analysed after the deadline is evaluated as exceeded and cannot be submitted without "
    "the H qualifier; with it, it is submitted. A result of the same sample for an analyte with "
    "its own, longer rule, analysed at the same time, is evaluated as met against that rule.",
    depends=("O03",),
)
def holding_rule(run):
    sample = Sample.objects.get(pk=run["sample_a4"].pk)
    admin = account(run, "admin")
    rule = HoldingTimeRule.objects.get(
        method__code=NUTRIENTS,
        method__version=VERSION,
        matrix="water",
        analyte=None,
        start_event="collection",
    )
    original = rule.duration
    browser = signed_in(run, "custodian")
    for duration in (original * 2, original):
        rule.duration = duration
        bench.save_config(
            actor=admin,
            instance=rule,
            reason=f"Synthetic: acceptance run sets the rule to {duration}.",
        )
        deadline = timezone.localtime(sample.collected_at + duration).isoformat()
        html = browser.get(f"/samples/{sample.pk}/").content.decode("utf-8", "replace")
        run.check(
            f'datetime="{deadline}"' in html,
            f"Rule set to {duration_label(duration)}: sample page deadline {deadline}",
        )
    analyst = account(run, "analyst")
    batch = bench.create_batch(
        actor=analyst,
        method=rule.method,
        matrix="water",
        prepared_at=timezone.now() - timedelta(minutes=30),
    )
    bench.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    revision = enter(run, sample, "Nitrate as N", "0.80", rl="0.05")
    run.check(
        revision.holding_status == "exceeded",
        f"Result analysed {timezone.localtime(revision.analysis_started_at):%Y-%m-%d %H:%M %z} "
        f"evaluated as '{revision.holding_status}': "
        + "; ".join(e.get("detail", "") for e in revision.holding_evaluation),
        "exceeded",
    )
    try:
        bench.submit_result(actor=analyst, revision_id=revision.pk)
        refusal = None
    except ValidationError as exc:
        refusal = " ".join(exc.messages)
    run.check(
        refusal is not None and "holding-time qualifier" in refusal,
        f"Submission without a holding-time qualifier refused: '{refusal}'",
        "the holding-time refusal",
    )
    own = HoldingTimeRule.objects.get(
        method=rule.method, matrix="water", analyte__name=OWN_RULE_ANALYTE, is_active=True
    )
    other = enter(run, sample, OWN_RULE_ANALYTE, "0.10", rl="0.02")
    run.check(
        other.holding_status == "met"
        and [e["rule_id"] for e in other.holding_evaluation] == [own.pk],
        f"{OWN_RULE_ANALYTE} result of the same sample evaluated as '{other.holding_status}' "
        f"against '{own.name}' ({duration_label(own.duration)}), not the method's "
        f"{duration_label(original)} rule",
        "met against the analyte's own rule",
    )
    qualified = enter(
        run,
        sample,
        "Nitrate as N",
        "0.80",
        rl="0.05",
        qualifiers=("H",),
        reason="Synthetic: holding time exceeded; qualified.",
    )
    bench.submit_result(actor=analyst, revision_id=qualified.pk)
    qualified.refresh_from_db()
    run.check(
        qualified.status == "submitted" and [q["code"] for q in qualified.qualifiers] == ["H"],
        f"Revision {qualified.number} with qualifier H submitted; revision {revision.number} kept",
    )


def guarded_rows(run):
    """(label, table, key column, key, column, new value) of history rows from this run."""
    v1 = ReportVersion.objects.get(report=run["report_a"], number=1)
    released = AuditEvent.objects.get(action="report.released", object_id=str(v1.pk))
    custody = CustodyEvent.objects.filter(sample=run["sample_a1"]).first()
    first = ResultRevision.objects.get(
        result__requested_test__sample=run["sample_a1"], analyte_name="Zinc", number=1
    )
    return [
        ("issued report PDF", ReportFile, "version_id", v1.pk, "pdf", b"%PDF-forged"),
        ("issued report version", ReportVersion, "id", v1.pk, "amendment_reason", "edited"),
        ("audit event", AuditEvent, "id", released.pk, "reason", "edited"),
        ("custody event", CustodyEvent, "id", custody.pk, "reason", "edited"),
        ("result observation", ResultRevision, "id", first.pk, "supplied_text", "999"),
    ]


# The history triggers raise with these SQLSTATEs (raise_exception, check_violation) and a
# message saying the row "cannot be" changed; only the SQLSTATE is written to the evidence.
TRIGGER_STATES = {"P0001", "23514"}


def refused_by_database(statement, params):
    """Run one SQL statement in a savepoint. Returns (SQLSTATE, whether the message is the
    history trigger's) for a refusal, or None when the statement went through."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, params)
    except DatabaseError as exc:
        return sqlstate(exc), "cannot be" in str(exc)
    return None


@step(
    "C06",
    "Issued reports and history cannot be changed",
    requirement="Issued report versions are immutable: later edits to the records do not change "
    "an issued PDF or snapshot, and the database itself refuses to update or delete history rows "
    "(outcomes 10, 11 and 13).",
    expected="After the client and project are edited with a reason, version 1's PDF and snapshot "
    "are unchanged; direct UPDATE and DELETE of the issued PDF, version, audit, custody and result "
    "rows are refused by the database; version 1 still downloads byte for byte.",
    depends=("O11",),
)
def immutability(run):
    manager = account(run, "manager")
    client = Client.objects.get(pk=run["client_a"].pk)
    receipt.update_client(
        actor=manager,
        client_id=client.pk,
        expected_revision=client.revision,
        reason="Synthetic: client moved after the report was issued.",
        address_line1="200 Changed Street",
    )
    project = run["project_a"].__class__.objects.get(pk=run["project_a"].pk)
    receipt.update_project(
        actor=manager,
        project_id=project.pk,
        expected_revision=project.revision,
        reason="Synthetic: permit label corrected after issue.",
        program="SYNTH-PERMIT-0002",
    )
    run.observe("Client address and project permit label changed with reasons after issue")
    stored = ReportFile.objects.get(version__report=run["report_a"], version__number=1)
    snapshot = hashlib.sha256(json.dumps(stored.snapshot, sort_keys=True).encode()).hexdigest()
    run.check(
        bytes(stored.pdf) == run["v1_pdf"] and snapshot == run["v1_snapshot"],
        f"v1 stored PDF and snapshot unchanged (snapshot SHA-256 {snapshot[:16]}…; it still prints "
        f"'{stored.snapshot['content'].get('project', {}).get('program', '')}')",
    )
    quote = connection.ops.quote_name
    for label, model, key, value, column, changed in guarded_rows(run):
        table, key, column = quote(model._meta.db_table), quote(key), quote(column)
        for action, statement, params in (
            ("UPDATE", f"UPDATE {table} SET {column} = %s WHERE {key} = %s", [changed, value]),
            ("DELETE", f"DELETE FROM {table} WHERE {key} = %s", [value]),
        ):
            refusal = refused_by_database(statement, params)
            run.check(
                refusal is not None and refusal[0] in TRIGGER_STATES and refusal[1],
                f"SQL {action} of the {label}: "
                + (
                    f"refused (SQLSTATE {refusal[0]}, "
                    f"{'the history trigger' if refusal[1] else 'not the history trigger'})"
                    if refusal
                    else "not refused"
                ),
                "refused by the history trigger",
            )
    response, body = download(run, "client_a", f"/portal/reports/{run['report_a'].pk}/v1.pdf")
    run.check(
        body == run["v1_pdf"], f"v1 downloads byte for byte as issued (SHA-256 {sha256(body)})"
    )
    events = AuditEvent.objects.filter(
        action__in=["client.updated", "project.updated"], reason__startswith="Synthetic"
    )
    run.check(
        events.count() == 2,
        f"{events.count()} audit events record the two edits with their reasons",
        "2",
    )


@step(
    "C07",
    "Forged form posts are refused",
    requirement="State-changing requests need the CSRF token and a matching origin "
    "(architecture: CSRF protection, explicit trusted origins).",
    expected="A signed-in project manager's form post without the token, and one with the token "
    "from a foreign origin, are both refused by the CSRF check (403) and create nothing; the "
    "same post with the token from the deployment's own origin creates the client.",
    depends=("O01",),
)
def csrf(run):
    browser = signed_in(run, "manager")
    data = {"name": "Forged Client (synthetic)"}
    without = browser.post("/clients/new/", data, csrf=False)
    foreign = browser.post("/clients/new/", data, origin="https://attacker.example.invalid")
    run.check(
        csrf_refusal(without)
        and csrf_refusal(foreign)
        and not Client.objects.filter(name="Forged Client (synthetic)").exists(),
        f"Without token: HTTP {without.status_code}; foreign origin: HTTP {foreign.status_code}; "
        "both the CSRF failure page; no client created",
        "the CSRF 403 twice",
    )
    genuine = browser.post("/clients/new/", data)
    run.check(
        genuine.status_code == 302 and Client.objects.filter(name=data["name"]).exists(),
        f"The same post with the token from the own origin: HTTP {genuine.status_code}, client "
        "created",
        "302",
    )


SOLIDS = "DEMO-TSS-AQ"  # the demonstration method whose results are worked from weights


@step(
    "C08",
    "A calculated result keeps its readings and can be worked out again",
    requirement="Where the laboratory sets a calculation for a test, the readings behind a "
    "result belong to the record: the result is worked from them at full precision, rounded "
    "once by the laboratory's rule, and can be worked out again from what was stored "
    "(docs/product.md, calculated results).",
    expected="For the demonstration solids test a typed final result is refused; the weights "
    "0.1183 g and 0.1058 g over 300 mL give 41.666... mg/L, reported as 42 by the demonstration "
    "rule (whole numbers below 1,000); the three readings are stored as typed; working the "
    "stored worksheet again gives the same result; the database refuses to change a stored "
    "reading; the result page shows the worksheet.",
    depends=("O03",),
)
def calculated_result(run):
    analyst = account(run, "analyst")
    now = timezone.now().replace(second=0, microsecond=0)
    tests = tests_for(SOLIDS)
    rule = CalculationRule.objects.get(definition=tests[0], is_active=True)
    sample = receive(
        run,
        run["order_a"],
        "OF001-A5",
        tests,
        collected_at=now - timedelta(hours=4),
        received_at=now - timedelta(hours=2),
    )
    batch = bench.create_batch(
        actor=analyst, method=Method.objects.get(code=SOLIDS, version=VERSION), matrix="water"
    )
    bench.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    result = Result.objects.get(requested_test__sample=sample)
    entry = dict(
        actor=analyst,
        result_id=result.pk,
        reporting_limit_applied="5",
        dilution_factor="1",
        analysis_started_at=now - timedelta(minutes=30),
    )
    try:
        bench.enter_result(supplied_text="42", **entry)
        refusal = None
    except ValidationError as exc:
        refusal = " ".join(exc.messages)
    run.check(
        refusal is not None and "calculated from readings" in refusal,
        f"A typed final result for the calculated test is refused: '{refusal}'",
        "the refusal of a typed result",
    )
    readings = {"gross": "0.1183", "tare": "0.1058", "volume": "300"}
    revision = bench.enter_result(supplied_text="", readings=readings, **entry)
    sheet = revision.worksheet
    stored = {r.key: f"{r.supplied_text} {r.unit}" for r in sheet.readings.all()}
    run.check(
        revision.supplied_text == "42"
        and revision.source == "calculated"
        and str(sheet.unrounded_value).startswith("41.6666666666")
        and stored == {"gross": "0.1183 g", "tare": "0.1058 g", "volume": "300 mL"},
        f"{sample.lab_id} {revision.analyte_name}: reported {revision.supplied_text} "
        f"{revision.unit} ({revision.get_source_display().lower()}) from readings {stored}; "
        f"value before rounding {sheet.unrounded_value}; calculation '{sheet.title}' version "
        f"{sheet.version}; rounding {rule.rounding.describe()}",
        "42 mg/L from the three stored readings",
    )
    again = worksheets.reproduce(sheet)
    run.check(
        again.available and again.matches and again.value_text == revision.supplied_text,
        f"Worked again from the stored readings: {again.value_text} {revision.unit}, "
        f"{'the same as' if again.matches else 'NOT the same as'} the recorded result",
        "the same result",
    )
    reading = sheet.readings.get(key="gross")
    refusals = [
        refused_by_database(
            f'UPDATE "{WorksheetReading._meta.db_table}" SET "value" = %s WHERE "id" = %s',
            [9, reading.pk],
        ),
        refused_by_database(
            f'DELETE FROM "{WorksheetReading._meta.db_table}" WHERE "id" = %s', [reading.pk]
        ),
    ]
    run.check(
        all(r is not None and r[0] in TRIGGER_STATES and r[1] for r in refusals)
        and WorksheetReading.objects.get(pk=reading.pk).supplied_text == "0.1183",
        "Direct UPDATE and DELETE of a stored reading: "
        + ", ".join("went through" if r is None else f"refused (SQLSTATE {r[0]})" for r in refusals)
        + "; the reading still holds 0.1183",
        "both refused by the database",
    )
    page = signed_in(run, "reviewer").get(f"/analytical/results/{result.pk}/")
    html = page.content.decode("utf-8", "replace")
    run.check(
        page.status_code == 200
        and "Worksheet: " in html
        and "0.1183" in html
        and "Same result" in html,
        f"Result page for the reviewer (HTTP {page.status_code}) shows the worksheet, the "
        "readings as typed and that working them again gives the same result",
        "the worksheet on the result page",
    )


SOIL_SOLIDS = "DEMO-SOLIDS-SOIL"  # percent solids, worked from three weights
SOIL_NITRATE = "DEMO-NITRATE-SOIL"  # worked from an extract reading to a dry-weight result


@step(
    "C09",
    "A soil result is taken back to the sample on a dry-weight basis, and its limit with it",
    requirement="Where a result is worked from a prepared portion, the reading is taken back "
    "to the sample with the amounts prepared and the reporting limit goes with it. A dry-weight "
    "result is worked with the sample's percent solids, which stays on record with its source "
    "(docs/product.md, calculated results).",
    expected="For the demonstration soil tests: dish weights of 21.4035 g, 31.6182 g and "
    "29.7391 g give 81.6 percent solids; an extract reading of 1.84 mg/L from 5.03 g brought to "
    "50 mL gives 22.4 mg/kg dry weight with a reporting limit of 0.609 mg/kg; the same entry is "
    "refused without the percent solids and on a wet-weight basis; the worksheet keeps the "
    "percent solids and the limit before rounding, and working it again gives the same result "
    "and limit.",
    depends=("O03",),
)
def dry_weight_result(run):
    analyst = account(run, "analyst")
    now = timezone.now().replace(second=0, microsecond=0)
    solids, nitrate = tests_for(SOIL_SOLIDS, "soil"), tests_for(SOIL_NITRATE, "soil")
    sample = receive(
        run,
        run["order_a"],
        "DB-A1",
        [*solids, *nitrate],
        collected_at=now - timedelta(hours=30),
        received_at=now - timedelta(hours=26),
        matrix="soil",
        source_type="soil",
        collection_location="Synthetic drying bed A1",
        preservation_check="Synthetic: cooled; no chemical preservation",
    )

    def batched(code, tests):
        batch = bench.create_batch(
            actor=analyst, method=Method.objects.get(code=code, version=VERSION), matrix="soil"
        )
        requested = sample.requested_tests.filter(definition__in=tests)
        bench.add_tests_to_batch(
            actor=analyst,
            batch_id=batch.pk,
            requested_test_ids=list(requested.values_list("pk", flat=True)),
        )
        return batch, Result.objects.get(requested_test__in=requested)

    drying, solids_result = batched(SOIL_SOLIDS, solids)
    weighed = bench.enter_result(
        actor=analyst,
        result_id=solids_result.pk,
        supplied_text="",
        readings={"tare": "21.4035", "wet": "31.6182", "dry": "29.7391"},
        reporting_limit_applied="0.1",
        dilution_factor="1",
        analysis_started_at=now - timedelta(hours=2),
        result_basis="wet",
    )
    run.check(
        (weighed.supplied_text, weighed.unit, weighed.source) == ("81.6", "%", "calculated")
        and worksheets.reproduce(weighed.worksheet).matches,
        f"{sample.lab_id} {weighed.analyte_name}: {weighed.supplied_text} {weighed.unit} from "
        f"the three weights (value before rounding {weighed.worksheet.unrounded_value})",
        "81.6 % from the three stored weights",
    )
    _, nitrate_result = batched(SOIL_NITRATE, nitrate)
    source = f"{sample.lab_id} percent solids, batch {drying.number}"
    entry = dict(
        actor=analyst,
        result_id=nitrate_result.pk,
        supplied_text="",
        readings={"reading": "1.84", "base_limit": "0.05", "final_volume": "50", "weight": "5.03"},
        reporting_limit_applied="",
        dilution_factor="1",
        analysis_started_at=now - timedelta(minutes=30),
    )
    solids_given = dict(percent_solids=weighed.supplied_text, solids_reference=source)
    refusals = {}
    for label, extra in (
        ("without percent solids", dict(result_basis="dry")),
        ("on a wet-weight basis", dict(result_basis="wet", **solids_given)),
    ):
        try:
            bench.enter_result(**entry, **extra)
            refusals[label] = None
        except ValidationError as exc:
            refusals[label] = " ".join(exc.messages)
    run.check(
        "required by this calculation" in (refusals["without percent solids"] or "")
        and "Choose Dry weight" in (refusals["on a wet-weight basis"] or "")
        and not nitrate_result.revisions.exists(),
        "; ".join(
            f"{label}: {'saved' if text is None else f'refused ({text})'}"
            for label, text in refusals.items()
        ),
        "both refused, nothing saved",
    )
    revision = bench.enter_result(**entry, result_basis="dry", **solids_given)
    sheet = revision.worksheet
    stored = {r.key: f"{r.supplied_text} {r.unit}" for r in sheet.readings.all()}
    run.check(
        (revision.supplied_text, revision.unit, revision.result_basis) == ("22.4", "mg/kg", "dry")
        and revision.reporting_limit_applied == Decimal("0.609")
        and (revision.percent_solids, revision.solids_reference) == (Decimal("81.6"), source)
        and stored.get("percent_solids") == "81.6 %"
        and stored.get("weight") == "5.03 g"
        and str(sheet.unrounded_limit).startswith("0.6090905547"),
        f"{sample.lab_id} {revision.analyte_name}: reported {revision.supplied_text} "
        f"{revision.unit} ({revision.get_result_basis_display().lower()}) with a reporting limit "
        f"of {revision.reporting_limit_applied.normalize()} from readings {stored}; value "
        f"before rounding {sheet.unrounded_value}, limit before rounding "
        f"{sheet.unrounded_limit}; percent solids source '{revision.solids_reference}'",
        "22.4 mg/kg dry weight, limit 0.609, with the percent solids on the worksheet",
    )
    again = worksheets.reproduce(sheet)
    run.check(
        again.available
        and again.matches
        and (again.value_text, again.limit_text) == ("22.4", "0.609"),
        f"Worked again from the stored readings: {again.value_text} {revision.unit} with a "
        f"reporting limit of {again.limit_text}, "
        f"{'the same as' if again.matches else 'NOT the same as'} the record",
        "the same result and limit",
    )


@step(
    "C10",
    "A titration keeps its burette and pH readings and its actual titrant strength",
    requirement="Titration results keep the original readings, calculation version and the "
    "laboratory's rounding so the reported value can be worked out again "
    "(docs/product.md, titration worksheets).",
    expected="The synthetic low-alkalinity test uses burette readings 0.20, 1.50 and 1.70 mL, "
    "200.0 mL sample and 0.0200 N acid to report 5.5 mg/L as CaCO3. A final pH that is not "
    "0.3 below the first is refused without saving. The accepted worksheet retains each "
    "original reading and reproduces the reported result.",
    depends=("O03",),
)
def titration_result(run):
    analyst = account(run, "analyst")
    now = timezone.now().replace(second=0, microsecond=0)
    tests = tests_for("DEMO-LOW-ALK-AQ", "water")
    sample = receive(
        run,
        run["order_a"],
        "TITRATION-A1",
        tests,
        collected_at=now - timedelta(hours=3),
        received_at=now - timedelta(hours=2),
        preservation_check="Synthetic: cooled; no chemical preservation",
        containers=[
            {
                "description": "Synthetic 500 mL HDPE",
                "condition": "Intact",
                "preservation": "Cool; no chemical preservative (synthetic)",
            }
        ],
    )
    batch = bench.create_batch(actor=analyst, method=tests[0].method, matrix="water")
    bench.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    result = Result.objects.get(requested_test__sample=sample)
    readings = {
        "initial": "0.20",
        "first_endpoint": "1.50",
        "final": "1.70",
        "sample_volume": "200.0",
        "normality": "0.0200",
        "first_ph": "4.60",
        "final_ph": "4.30",
    }
    entry = dict(
        actor=analyst,
        result_id=result.pk,
        supplied_text="",
        reporting_limit_applied="0.5",
        dilution_factor="1",
        analysis_started_at=now - timedelta(minutes=30),
    )
    refused = False
    try:
        bench.enter_result(**entry, readings={**readings, "final_ph": "4.20"})
    except ValidationError as exc:
        refused = "reading_final_ph" in exc.message_dict
    run.check(
        refused and not result.revisions.exists(),
        "The wrong pH drop was refused without a result revision",
    )
    revision = bench.enter_result(**entry, readings=readings)
    stored = dict(revision.worksheet.readings.values_list("key", "supplied_text"))
    again = worksheets.reproduce(revision.worksheet)
    run.check(
        (revision.supplied_text, revision.unit, revision.source)
        == ("5.5", "mg/L as CaCO3", "calculated")
        and stored == readings
        and again.matches
        and again.value_text == "5.5",
        f"Reported {revision.supplied_text} {revision.unit}; all seven supplied readings "
        "retained, and the recorded calculation and rounding give the same result",
        "5.5 mg/L as CaCO3, with all readings retained and reproducible",
    )


@step(
    "C11",
    "A BOD result keeps selected and excluded bottle observations",
    requirement="A BOD worksheet retains original bottle readings, seed correction evidence, "
    "analyst selection, calculation version and rounding so its mean can be reproduced "
    "(docs/product.md, BOD worksheets).",
    expected="Two synthetic bottles with 300 mL filled volume and 30 or 15 mL sample, "
    "initial DO 8.00 mg/L, final DO 4.50 or 6.00 mg/L and 0.50 mg/L seed correction "
    "give a selected mean of 30.0 mg/L. A fully depleted excluded bottle and its reason "
    "are retained. Nonzero correction without a source is refused without saving.",
    depends=("O03",),
)
def bod_result(run):
    from analytical.synthetic_dataset import BOD_BOTTLES

    analyst = account(run, "analyst")
    now = timezone.now().replace(second=0, microsecond=0)
    tests = tests_for("DEMO-BOD-AQ", "water")
    sample = receive(
        run,
        run["order_a"],
        "BOD-A1",
        tests,
        collected_at=now - timedelta(hours=3),
        received_at=now - timedelta(hours=2),
        preservation_check="Synthetic: cooled; no chemical preservation",
        containers=[
            {
                "description": "Synthetic 1000 mL HDPE",
                "condition": "Intact",
                "preservation": "Cool; no chemical preservative (synthetic)",
            }
        ],
    )
    batch = bench.create_batch(actor=analyst, method=tests[0].method, matrix="water")
    bench.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(sample.requested_tests.values_list("pk", flat=True)),
    )
    result = Result.objects.get(requested_test__sample=sample)
    entry = dict(
        actor=analyst,
        result_id=result.pk,
        supplied_text="",
        reporting_limit_applied="2.0",
        dilution_factor="1",
        analysis_started_at=now - timedelta(minutes=30),
    )
    missing_source = [dict(row) for row in BOD_BOTTLES]
    missing_source[0]["seed_reference"] = ""
    refused = False
    try:
        bench.enter_result(**entry, bottle_data=missing_source)
    except ValidationError:
        refused = True
    run.check(
        refused and not result.revisions.exists(),
        "Nonzero seed correction without evidence was refused without a result revision",
    )
    revision = bench.enter_result(**entry, bottle_data=BOD_BOTTLES)
    stored = revision.worksheet.bottle_data
    again = worksheets.reproduce(revision.worksheet)
    run.check(
        (revision.supplied_text, revision.unit, revision.source) == ("30.0", "mg/L", "calculated")
        and stored == BOD_BOTTLES
        and stored[2]["selected"] is False
        and bool(stored[2]["reason"])
        and again.matches
        and again.value_text == "30.0",
        f"Reported {revision.supplied_text} {revision.unit}; all original bottle strings, "
        "seed correction sources and the excluded bottle reason retained; mean reproduces",
        "30.0 mg/L, with selected and excluded observations retained and reproducible",
    )
