# Workflow rules and limits

What the application does at each step of laboratory work, and the limits a laboratory should
know before relying on it. The help panel inside the application describes every page, field and
action; this is the overview. [Product scope](product.md) explains why the rules exist.

Synthetic catalog values in the demo seed are demonstration data, not approved methods or limits.

## Time zones

All collection and receipt times are interpreted in the configured lab time zone and stored with
time zone support. Naive form and CSV timestamps use the deployment `TIME_ZONE`; explicit UTC
offsets retain their instant. `Lab.timezone` is a recorded setup copy, not a parsing override.
New reports freeze the chosen zone and timestamp offsets; issued reports retain their original
presentation even if deployment settings later change.

## Clients, projects and chains of custody

Client records hold the mailing and invoicing details, projects the program, site, standing
purchase order and laboratory project manager. Receipt lists (custody seal, preservation check,
container type, preservative, condition) are starter vocabularies with an "Other" entry; edit
`receipt/vocabulary.py` to suit the lab. They are not method requirements.

Start a chain of custody under **Orders**, select its project and enter the COC reference once.
Record its purchase order, turnaround, shipment and instructions there; **Edit details** changes
them later with a reason.
Receive each sample from that order, recording its containers and requested tests, then continue
with the next sample. New accessions use `VR-000001` style numbers; gaps are normal and existing
IDs remain unchanged. Custodians and project managers can resolve a held sample with a recorded
reason. Project managers keep clients and projects at `/clients/<id>/`, which lists their projects,
chains of custody, quotes, submissions, portal accounts and recent changes, and holds additional
contacts. Changes keep their history. The demo seed includes synthetic water, wastewater and soil
methods, analytes and QC settings; existing records and laboratory edits are retained.

A project manager or administrator can set an **Account hold** with an **Account hold note**
on the client. It shows a staff notice at receipt and on the release preview, asking staff to
check with the project manager. This is information only: it does not block either action or
replace sample dispositions, QC checks or release authority. Clearing the hold retains its note
unless it is edited; the reason and old/new values stay in the client's history. Hold notes do
not appear in the client portal, report snapshots, PDFs or deliverables.

## Draft invoices

Project managers and administrators prepare an internal draft from a received chain of custody
and an explicitly selected active price list or accepted quote version. Each current requested
test contributes one line; missing prices prevent creation. The draft records its billing details, source identities and
prices at creation, so changes to the order, client, requested tests or price list do not silently
change its amounts. Sample disposition is evidence for the preparer to consider, not a billing
decision. Staff can change billing details and line descriptions, quantities and prices with a
reason, or cancel a draft while preserving its history. Only one active draft is allowed per
order. An accepted quote must belong to the same client and this project or no project. Its
entire set of test definitions and quantities must match the received order, one quote line per
test definition; ambiguous or incomplete test coverage is refused. Each quoted fee or discount
is copied once after the test lines, with its quantity and signed whole-cent USD price. Test
and fee prices must be nonnegative; discount prices must be zero or negative. The exact total
must stay nonnegative and the combined draft may have at most 200 lines. Kind, quote version
and line provenance remain with the draft through reasoned edits and cancellation. Quantities are matched per order, not consumed across
orders, and quote terms do not replace the billing snapshot. Staff can also add a fee or discount
with a reason; it is marked Manually added, with no sample, requested test or quote link.
Additions use the same amount and line bounds, reject stale or cancelled drafts and retain
attributed before/after history. Every line remains present: correct an unwanted amount to zero
with a reason. Staff may also set, change or clear an optional due date with a reason and find
drafts within an inclusive date range. Dates are entered explicitly, never calculated from
payment terms, and do not establish an outstanding balance. Drafts stay internal; issued
invoices and accounting exports remain planned.

## Spreadsheet import of setup records

**Setup → Spreadsheet import** loads clients, projects, analytes, methods, tests, QC
requirements, holding-time rules, QC limits and preservation rules from CSV files, one file per
record type. Project managers import clients and projects; administrators import every type.
Save the sheet as CSV (UTF-8): workbooks are not read, because they hand numbers over as binary
floating-point values and limits must stay exact decimals. A file holds up to 1024 KiB and 2000
rows.

Rows find existing records by the keys people use, never database IDs: account code for a
client, client account code and name for a project, analyte name, method code and version, and
those plus analyte and matrix for a test. A matching row changes the record; any other row
creates one. A column left out of the file leaves values alone; a blank cell clears an optional
value. A row may refer only to records that already exist, so import analytes and methods before
tests, tests before limits and rules, and clients before projects.

Every row goes through the same service as manual entry, with its checks, identity locks and
history. The preview runs those services and undoes the work, then shows each row's action, the
values before and after, and every error. Import checks the file again, refuses it if anything
it changes was edited since the preview, and saves all rows or none. A file that changes existing
records needs one reason; each record's history names the file and its SHA-256 digest, and one
further event records the file and its counts. The uploaded file is not kept. Contacts, price
lists, qualifiers, preparation requirements, users, samples and results are not imported here.

Starter files on the same page hold common method, test and rule entries read from 40 CFR
136.3 Tables IB and II, EPA methods and SW-846 Chapter Three on 2026-11-12. Each rule's basis
names its source. The Table II rows behind the per-analyte holding rules were compared again
on 2026-11-14 with the eCFR text current to 2026-11-12. They are starting points, not laboratory policy: the laboratory checks them
against its methods, permits and current rules and edits them before importing. They carry no
reporting limits, and Table II's 6 months is entered as 180 days for the laboratory to confirm.

A holding-time rule belongs to a method and matrix and applies to every analyte of the method
unless it names one. A rule that names an analyte applies to that analyte only and replaces,
for it, that method's method-wide rule with the same start and end events; the method's other
clocks still apply, and so do the rules of a preparation method. Result entry, the worklist,
the sample page and the batch test picker resolve the rule the same way, and an evaluation
recorded on a result is not recalculated when rules change. Table II sets its limits per
parameter, so where one method covers parameters with different limits (EPA 300.0 and EPA
365.1 in the starter files) the method-wide starter rule carries the shortest limit and each
analyte with a longer one has its own rule.

Limits: a rule names one analyte or none, never a group, and an analyte's rule needs a test of
that method and matrix (or one the method prepares). An analyte added to a method later is
held to the method-wide limit until it has a rule of its own. The starter files still leave
out EPA 200.7 total phosphorus and EPA 353.2 nitrite, whose limits differ from the rest of
their method.

## Receipt corrections

Use **Correct sample details** for reasoned receipt corrections: identity, collector, location,
collection times, receipt observations, requested tests (add, or withdraw when no result, external
dispatch or QC parent depends on it) and matrix. Each change needs a reason and keeps earlier values.
Collection clocks, observations and matrix lock once analytical, QC or external work exists. Tests
added after report issue are reported separately; issued reports never change. Corrections are
refused after physical disposal and never rewrite custody or source observations. Reprint affected
labels as needed. Correct a mistyped COC reference from the order page until an issued report lists
the order. An empty chain of custody, or an accepted client submission that never arrives, can be
cancelled and reinstated with a reason.

## Preservation and preparation at report issue

Every test in a new report issue or amendment is checked against current preservation evidence,
including previously issued results that have not changed. Missing, manual, outside-limit and
configured narrative findings need a current reasoned disposition; a hold or reject decision blocks
issue. The first amendment of legacy work may therefore need assessments. Changes to the receipt,
the test or its rule, including requirement, source or SOP wording, invalidate earlier matching
assessments. Historical issued PDFs, snapshots and downloads stay unchanged and are never gated.

A test's preparation requirement applies to new result entry and new approval only. An already
approved result with no preparation link, or whose method changed since, can still be released or
amended; its report shows the preparation identity as missing and nothing is backfilled. Existing
linked identity, chronology, cancellation and QC checks still gate. Cancelled physical aliquots are
counted conservatively, and labels state the count. Exports (native and NYSDEC EDD) carry the explicit
actual preparation identity; a missing QC run blocks the EDD.

The NYSDEC EDD is exploratory and requires recipient validation; it is not an accepted regulatory
submission.

## Laboratory identity on reports

The `admin` role keeps the laboratory's address, phone, email, website, logo and certificates
under **Setup → Laboratory profile**. Other staff can read the page, and the report pages link
to it when a certificate is left off. The laboratory name is set with `setup_lab`. Reports
issued from then on print a letterhead on the first page and, near the end, the certificates
marked **Print on reports**. Issued reports keep what they printed, and the next amendment's
change list names the laboratory details that differ.

Certificates are printed as the laboratory recorded them: issuing authority, program or scope,
certificate or laboratory ID and validity dates. A marked certificate is printed only on a report
issued within its recorded validity (both dates inclusive; a blank date is no bound). The report
preview names any marked certificate that is left off, and that never blocks a release. The
application keeps no scope register and never checks a result against a certificate. Under the
list it prints the laboratory's own certificate statement or, when none is recorded, a sentence
saying that results were not checked against any scope. The same list goes on every report;
there is no selection per report, client or program. Whether to print a certificate, and how
results outside its scope are identified, is the laboratory's decision under its accreditation
body's rules. A subcontract laboratory's certificate number is not recorded or printed.

The logo is a PNG or JPEG file of at most 512 KiB, 32 to 2000 pixels on each side. It is the
laboratory's own logo; an accreditation body's symbol is the laboratory's to use under that
body's rules. Uploaded logos are kept after they are replaced or removed, so a report can always
be reproduced as issued.

## Delivery schedule

Set an optional delivery target on the chain of custody, then use **Delivery schedule** to
prioritize received tests by due date and stage. Held work remains visible; approved results
stay outstanding until their current revision is issued. Changes to a target require a reason
and retain history. Targets are explicit client commitments, separate from method holding times;
project turnaround days do not silently create a calendar or business-day deadline.

## Labels

Order and sample **Labels** menus produce sample or container identity PDFs, one 4 × 2 inch
label per page. Print at actual size and verify your printer/stock alignment. Barcodes encode
the sample accession or a stable container identifier; printing does not record receipt or
change custody. Up to 200 labels can be downloaded at once.

## Subcontracted tests

**Subcontracted tests** records completed handoffs of selected containers to an external
laboratory. External observations retain their reported method, unit, qualifiers, analyst and
source PDF; they do not create internal analytical batches or imply internal QC approval.
A different reviewer checks the source before report release. Corrections add source versions
and require a report amendment. Original external PDFs stay staff-only because they may contain
other clients' information. Cancelling an unreported subcontract does not record a bottle return.

## Sample storage and disposal

Physical **Sample storage** records a laboratory-selected retain-through date, a separate disposal
hold and attributed disposal. Dates are inclusive; no retention period is inferred. Outstanding
latest results must be issued before disposal unless the sample was rejected, and receipt/disposal
holds must be resolved first. Disposal keeps electronic custody, analytical and report history;
it never deletes records or claims that the lab has satisfied a regulatory retention period.

## Result imports

Analytical **Import mappings** supports explicitly configured long or wide CSV/TSV layouts and
plain XLSX worksheets, with an exact visible worksheet selected for each upload.
Preview the mapping against the target batch before importing. Each import retains the source
hash and mapping used; changing a saved profile does not reinterpret previous observations.
XLSX imports stored numeric text without binary-float conversion or display formatting. Text
identifiers and analysis timestamps are required; formulas, merged cells, typed dates and error
cells in the selected sheet are refused. Hidden rows and columns are included. The original
workbook is not retained; its hash, worksheet and source rows are recorded. Repeated-file checks
include the worksheet, and confirmation rechecks source selection as well as current results.
These are operator-defined mappings, not validated vendor instrument adapters. Legacy XLS and
macro-enabled workbooks remain outside this workflow. Setup spreadsheet import remains CSV-only.

**Export run list** on an analytical batch downloads a generic CSV for analysts and administrators.
It covers the entire current batch, including tests hidden by filters or pagination, followed by
its recorded QC samples. Sample and QC rows are distinct; each sample test is a row, so a sample
requested for several analytes repeats. Configured reporting limits are not the limits applied
to entered results. Preparation identifiers describe the latest revision in this batch, not a
selection for the next run. QC rows name only the recorded sample, type and optional parent;
no analytes, missing standards or injection sequence are inferred. Map the columns and check the
sample states against the intended instrument run. This download changes no record and is not
retained as an issued snapshot. Its complete column reference and refusal states are in the
batch's Help drawer.

### Recorded run calibration

On an analytical batch, **Record calibration** keeps an instrument-reported curve for one test:
the source reference, fit/equation description and standard points exactly as entered, with
concentration and response units. This is separate from equipment maintenance/calibration events.
It does not fit a curve, judge its quality or assert that a method's calibration requirements were
met. A closed batch still accepts evidence because closing freezes membership only.

The single-result entry/correction form offers optional **Calibration** and **Calibration
verification** choices for its batch and test. The check must be a current calibration-verification
QC observation, after the curve; a closing check may follow sample analysis. The curve must
precede or coincide with analysis and units must match. Grid, file import and BOD bottle entries
do not associate calibration. A later correction does not infer or automatically carry a curve
forward: select the intended evidence explicitly.

Records cannot be edited or deleted. Correct a curve by recording another reference, then enter
a new result revision with the intended evidence and a correction reason. Prior revisions keep
their original curve, check and snapshot. If a selected QC observation is corrected, a fresh
result revision must select the intended current evidence before submission, approval or release.
People who entered the selected curve or check cannot review or release its result. All existing
QC, preservation and holding-time gates still apply; selecting a check never bypasses them.

## Calculated results

A result can be worked out by the application from the readings taken at the bench, in place
of a final value typed in. The `admin` role sets this per test under **QC configuration →
Calculation rules**: the calculation, and how its value is rounded. With an active rule, manual
entry of that test (the entry grid and the result page) takes the readings and refuses a typed
result. The application carries the arithmetic at full precision, rounds once at the end and
stores a worksheet with the result: each reading as typed with its unit, the calculation and its
version, the value before rounding and the rounding applied. The result page shows the worksheet
and works it again from the stored readings each time it is opened. Worksheets are never
changed: a correction adds a revision with its own worksheet, and a later change to the rule
leaves earlier results as they were.

Calculations available:

| Calculation | Readings | Result | Reporting limit applied |
| --- | --- | --- | --- |
| Reading times dilution factor | Instrument reading (or ND), reporting limit before dilution, dilution factor | Reading × dilution factor | Limit before dilution × dilution factor |
| Solids by weight, balance in grams or in milligrams | Dried residue with its filter or dish, filter or dish alone, sample volume in mL | (Residue weight) × 1,000,000 ÷ mL for grams, × 1,000 ÷ mL for milligrams, in mg/L | Typed by the analyst |
| Preparation by volume | Instrument reading (or ND), reporting limit in the solution, sample volume prepared and final volume in mL, dilution factor | Reading × final volume ÷ sample volume × dilution factor, in the unit of the test | Limit in the solution × the same factors, rounded like the result |
| Prepared solid as weighed (mg/L to mg/kg, ug/L to ug/kg, ug/L to mg/kg) | Instrument reading (or ND), reporting limit in the solution, final volume in mL, sample weight in g, dilution factor | Reading × final volume ÷ sample weight × dilution factor (÷ 1,000 from ug/L to mg/kg), on the basis of the portion as weighed | Limit in the solution × the same factors, rounded like the result |
| Prepared solid corrected to dry weight (the same three pairings) | The same, with the weight as received, and the percent solids and its source from the entry | The same ÷ (percent solids ÷ 100), on a dry-weight basis | Limit in the solution × the same factors, rounded like the result |
| Percent solids by weight, percent moisture by weight | Dish alone, dish with the sample as received, dish with the dried sample, in g | Dried sample ÷ sample as received × 100, or water lost ÷ sample as received × 100, in % | Typed by the analyst |
| Alkalinity by titration to pH 4.5 | Initial and final burette readings (mL), original sample volume (mL), standardized acid normality (N), endpoint pH | (Final − initial) × normality × 50,000 ÷ sample volume, in mg/L as CaCO3 | Typed by the analyst |
| Low alkalinity by two-endpoint titration | Initial, first endpoint and final burette readings (mL), original sample volume (mL), standardized acid normality (N), both endpoint pH readings | (2B − C) × normality × 50,000 ÷ sample volume, where B and C are volumes from the initial reading to each endpoint, in mg/L as CaCO3 | Typed by the analyst |
| Total hardness by EDTA titration | Initial and final burette readings (mL), original sample volume (mL), standardized EDTA normality (N) | (Final − initial) × normality × 50,000 ÷ sample volume, in mg/L as CaCO3 | Typed by the analyst |
| BOD bottle worksheet | Per bottle: identifier, initial/final DO and entered seed correction (mg/L), correction source, sample and filled bottle volumes (mL), selection and exclusion reason; common preliminary dilution | Mean of selected ((initial DO − final DO − seed correction) × bottle volume ÷ sample volume) × preliminary dilution, in mg/L | Typed by the analyst |

A preparation calculation takes the reading back to the sample with the amounts actually
prepared, and takes the reporting limit with it. The limit it works out is rounded by the test's
rounding rule, the same rule as the result, and the worksheet keeps the limit before rounding;
a rounding that would take the limit to zero is refused at entry. Working a worksheet again
checks the limit as well as the value.

For soil, the correction to dry weight is a calculation the laboratory chooses for the test, never
something inferred from the basis: a method that weighs the portion as received and divides by
percent solids uses a "corrected to dry weight" calculation, and a method that dries the portion
before weighing uses "as weighed" and declares dry weight. A corrected result must be entered
as dry weight with the percent solids and where it came from, and the percent solids is kept on
the worksheet with the other readings. An "as weighed" calculation never applies percent
solids, even where one is recorded beside the result: declaring dry weight there says that the
portion was dried before it was weighed. Percent solids and percent moisture are shares of the
sample as received and are entered as wet weight; weights in which the dried sample is lighter
than the dish or heavier than the sample as received are refused. The entry pages offer the
basis the calculation means; the analyst can still change it and is then told why it does not
fit. A preparation by volume takes a volume of sample and cannot be chosen for a soil test.

Rounding is the laboratory's own rule and has no default: significant figures or decimal
places, an optional second rounding from a size the laboratory names (for example whole numbers
below 1,000 and three significant figures from there up), and whether a dropped 5 rounds to the
even digit or always up. Which of the two roundings applies is decided by the value before
rounding, so a value just under that size can round up to it and still show the digits of the
smaller range. Results keep at most 10 decimal places; a rounding that would need more for a
value is refused when the value is entered. A calculated value below the reporting limit applied needs a qualifier,
or the analyst reports it as ND; the readings and the calculated value stay on record either
way. A reading of ND in the dilution calculation gives ND at the diluted limit.

Limits: a rule covers one test and one calculation, and its results are always worked from
readings in manual entry, so one test is either corrected to dry weight or not. File imports are
not recalculated or rounded: they supply final values, kept with their source. QC sample
measurements are still typed. The solids calculation gives mg/L only, applies no blank
correction and no dilution factor, and does not scale the reporting limit with the volume used.
The preparation amounts are typed with each result; they are not recorded once on a prepared
aliquot and shared by its tests. The percent solids of a dry-weight result is typed with its
source; it is not taken from the sample's percent solids result, and a later correction of that
result does not flag the results worked with the earlier value. The limit in the solution is
typed; the catalog reporting limit is not scaled from nominal amounts. The limit worked out is rounded
by the same rule as the result, which can round it down as well as up; a laboratory that rounds
its limits another way cannot set that yet. A calculation gives one
unit from one unit: the three pairings above for solids, the unit of the test for a preparation
by volume and percent for solids and moisture. Units are matched by name, and only the spelling
of micro (µ, μ or u) may differ. Each reading is used as typed, so a method that rounds the
solution concentration first is followed by typing the rounded concentration. Other titrations are not built. The arithmetic of a calculation version is part of the
release: a laboratory checks it against its method before use, and the demonstration rules are
synthetic.

BOD bottles are entered from the result’s **Record BOD5 bottles** action, reached from the batch entry grid or the result record. Keep 1–12 complete bottle rows; every selection is explicit and excluded bottles need a reason. The worksheet keeps all rows and shows their calculated concentrations, including excluded negative values. A selected bottle must have nonnegative corrected depletion. Each bottle is weighted equally in the mean; the sample fractions are applied before averaging. The common dilution factor is only a preliminary dilution before aliquots were taken, not the bottle-to-sample ratio again. Enter zero seed correction for an unseeded bottle, or enter the correction for that bottle with a source reference. Seed correction is supplied in mg/L of bottle oxygen demand, not in mg/L of final sample result. The application does not calculate it from seed controls, subtract a dilution-water blank or choose usable bottles. Review method suitability, incubation, depletion, residual oxygen and QC separately. If no bottle can be selected, this calculation cannot produce the method-specific exception report. Corrections keep the earlier worksheet and refuse a save if the result changed since the form was opened.

The titration worksheets use one uninterrupted burette run, with nonnegative, increasing
readings (equal initial and final readings are permitted for the single-endpoint equations).
The low-alkalinity second endpoint must follow the first. Sample volume and titrant normality
must be positive. Alkalinity uses an unaltered sample with no added dilution water. For
hardness, sample volume excludes reagents and water added in the titration vessel. Normality is equivalents per liter, not molarity or mg CaCO3 per mL.
Alkalinity to pH 4.5 requires that recorded endpoint; the low-alkalinity equation requires a
first endpoint from 4.3 to 4.7 and a final pH exactly 0.3 lower. No tolerance is inferred.
A negative low-alkalinity result is retained, with the usual below-limit qualifier or ND
choice; it is never silently changed to zero. The administrator selects the calculation per
test, so the application does not switch procedures based on a result. These calculations
require the test unit mg/L as CaCO3 and a nonsoil matrix. They apply no dilution, preparation
or blank correction and do not scale the reporting limit. They exclude the pH 3.9 mineral-acid
procedure and the separate low-hardness blank procedure. Endpoint suitability, titrant
standardization and method acceptance remain laboratory checks; the source text is named
on each worksheet and in the [product evidence](product.md#primary-sources).

## MDL studies

MDL studies show **Recalculate by** as 13 calendar months (lab time zone) after the calculation date of
the latest adopted or retained calculation; review and adoption do not restart it. Confirm the interval
against your method and accreditor.
Routine MDL blank import records an explicit source basis, defaulting to the current preparation
requirement. A combined or full-procedure basis needs a stored reason, and the observation freezes its
source batch and actual analytical run. Observations recorded before this show the basis as not
recorded; they are never reclassified.

## Soil results

New internal soil observations require an explicit wet- or dry-weight basis. Optional supplied
percent solids must include its measurement/source reference; blank means not recorded. Typed
and imported results preserve the final value and applied reporting limit without conversion.
The one conversion is a result worked from readings by a calculation that corrects to dry weight
(see Calculated results): it requires the dry-weight basis and the percent solids with its
source, and keeps both with the worksheet.
Earlier observations retain their unknown basis; corrections record new metadata without
rewriting prior revisions or issued PDFs. External results remain as supplied.

## Electronic record retention

Staff can read **Electronic record retention** at `/lab/record-retention/`. It starts as **Not
recorded**. A deployment administrator with the `admin` role records or clears the laboratory's
statement under **Administration → Core → Lab**, with a required reason and stale-edit protection.
Changes preserve the prior statement and attribution. No duration or automatic deletion is inferred.

## Announcements

The `admin` role posts announcements under **Setup → Announcements**. Each has a title, a
plain-text message, an importance (Notice or Important) and one or more places: the sign-in page,
the staff dashboard and the client portal home page. No other page shows them. Who sees one
follows the account, not the page: a visitor who is not signed in sees only sign-in page
announcements, staff only staff ones, and client users only client ones, which are the same for
every client. Anything on the sign-in page is public, so keep confidential and client
information out of it. Messages are shown exactly as typed; formatting, links and HTML are not
interpreted.

An announcement shows from **Show from** until **Show until**, or until it is withdrawn when no
end is set. Withdrawing is permanent: the announcement stays in the list with its history and
cannot be changed or shown again. Announcements are never deleted, there is no draft or approval
step, readers cannot dismiss them, and nothing is emailed. Posting, every change and withdrawal
are audited with the administrator and the values before and after.

`seed_demo --announce-accounts` posts one sign-in page announcement that lists the demo accounts
and their shared password, for a public demonstration; see the README. It never lists
`demo_admin` and refuses to publish a password that opens that account. The listed accounts
become shared demonstration accounts: whoever signs in to one cannot open its password, email
address or two-factor pages, and no password-reset link is mailed for it. Nothing else sets that
restriction, and no page removes it.

## Tables and page help

Table search, filters, sorting and pagination run on the server. DataTables/ColReorder enhance
column visibility and order. Column layout, rows per page and sort are saved per user on the server
(`core/preferences.py`), never in the browser, because workstations are shared. Vendor versions and licenses
are included under `static/vendor/datatables/`.

The documentation icon at the top right opens help for the current page or form in a resizable
right panel. It explains the page's purpose, actions, fields and workflow rules. The panel follows
navigation and remembers its width and open/closed state for each account in this browser.
Drag its left edge or focus the separator and use the arrow keys to resize; use its close button
or Escape from inside the panel to close it. Contributors use the
[`document-page` skill](../.agents/skills/document-page/SKILL.md) to update help with each page change.
