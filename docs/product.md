# OpenBGA Environmental LIMS: product scope

OpenBGA Environmental LIMS is a free, open-source laboratory information management system (LIMS) for US environmental testing labs. This document says what the product must do, why, and in what order. It does not prescribe architecture. Domain claims cite public sources, linked under Evidence and limits so they stay checkable after cloning. Researched 2026-11-08.

## Working assumptions

Confirmed: lab type (commercial US water, wastewater and soil lab) and one lab organization per deployment. Everything else, including first-release program and discipline focus, is a working default that can widen later (see Project decisions).

| Assumption | Consequence |
|---|---|
| Commercial US water, wastewater and soil lab | First release works chemistry first (metals, nutrients, solids, BOD; organics later). Drinking water compliance, microbiology and other disciplines are deferred, not excluded, and the model should not block them |
| One lab organization per deployment | No tenant isolation layer. Clients are records inside the lab, not separate tenants |
| Staff roles plus client access | Roles: admin, sample custodian, analyst, reviewer, project manager. Authorization is a permission a lab grants to qualified staff, which may be the same people as reviewers. Clients see only their own work |
| Samples come from clients under a chain of custody (COC) | Receipt is a formal, recorded event, not a status flip |
| Results may feed permit reporting | Records must stand up to a permit or accreditation audit, but see "What this product claims" |

## What this product claims

OpenBGA Environmental LIMS supports a lab's own quality system. It does not make a lab compliant, accredited or certified. Accreditation (for example under the National Environmental Laboratory Accreditation Program, run by The NELAC Institute, TNI) and drinking water certification belong to the lab and its accreditation body. Do not write UI copy, README text or reports that claim compliance. Say "supports" and name the record or control.

Restricted standards (TNI, ISO/IEC 17025) are not reproduced here or in code. Requirements below come from public federal rules and EPA material, plus paraphrased public interpretations.

## The first journey

One complete path must work end to end before any breadth is added. A thin slice of every module is not the goal.

```
client + project -> order from COC -> receipt and inspection -> samples/containers
-> tests scheduled -> analytical batch with QC -> results -> QC and holding-time evaluation
-> analyst submit -> technical review -> approval -> versioned final report -> client view
                                                        (amendment creates a new version)
```

Acceptance outcomes for the first usable release:

1. A project manager creates a client, contact and project with a permit or program label, required turnaround, and report preferences.
2. A custodian enters a COC as an order: sample IDs, collection date and time, matrix, requested tests, collector name.
3. At receipt the custodian records cooler temperature, seal condition, container condition, field preservation checks and any discrepancy. Each discrepancy needs a disposition (proceed, qualify, hold, reject) and a note. Lab sample IDs are assigned.
4. Each sample shows its holding-time clocks and flags samples at risk or expired. Expiry is computed from the rules, never typed.
5. An analyst builds a batch for one method, adds samples and required QC samples, and enters or imports results.
6. Results carry value, unit, qualifier, reporting limit used, dilution, analysis date and time, analyst and method version. Non-detects are stored as non-detects, not as zero.
7. QC samples are evaluated against configured limits. A batch with failing QC cannot be approved without a recorded corrective action or disposition.
8. A qualified person other than the analyst performs technical review. Nobody reviews their own results. Reviewers can return results with a reason, which creates a visible re-analysis or correction trail.
9. Release is an explicit, recorded authorization by a user holding that permission: who, when and what was authorized. The reviewer may hold it. No extra approval step is required by default.
10. The system produces a final report as a stored, immutable PDF with a unique ID and version.
11. A change after issue produces a new report version that references the original, states the reason and shows what changed. The original stays retrievable.
12. A client user signs in and sees only their own projects, sample status and issued reports.
13. Every change to a result, status, limit or report is traceable: who, when, old value, new value, reason.

A demo seeded with synthetic data (invented clients, sites and values) must complete this journey. Use clearly fake names such as "Example Utilities" or "Test Creek Outfall 001".

## Domain rules that must be right

These are the rules where a plausible but wrong implementation misleads a lab. Details beyond these are for implementers to research per method.

| Topic | Rule | Source |
|---|---|---|
| Holding time start | Grab sample: clock starts at collection. Composite: at end of collection. So collection date and time (with time zone) are required fields | 40 CFR 136.3 Table II fn. 4 |
| Holding time end | Limit is time to the start of analysis, not to completion. Some limits are short: pH and total residual chlorine within 15 minutes, E. coli incubation start within 8 hours (Clean Water Act methods) | 136.3 Table II, fn. 22 |
| Model shape | Express a holding rule as start event, end event and duration, so separate preparation and analysis limits (common in organics) also fit. This is general practice, confirm per method | Not verified |
| Exceeding a hold | For Clean Water Act compliance monitoring, results past the limit are not valid unless stability data and a regional variance exist. Other programs differ. The product flags and qualifies. It never silently passes or deletes | 136.3 fn. 4, 136.3(e) |
| Preservation | Preservation is a per-method rule, not a lab-wide policy. Where a method's rule requires cooling to 6 C or below without freezing (many Clean Water Act aqueous methods), that rule forbids rounding a reading down to pass. Always keep the original reading, with unit and instrument or thermometer if known, and evaluate it against the rule of each requested method. Some tests have no temperature requirement, and other methods use different limits | 136.3 fn. 18 |
| Examples | Nitrate and BOD: cool, 48 hours. Most metals: nitric acid to pH below 2, 6 months. Mercury (cold vapor atomic absorption): 28 days. Illustrative only, boron, hexavalent chromium and other analytes differ | 136.3 Table II |
| Method-specific | Containers, preservation and holding times differ by method and matrix. Seed a small editable set, never hard-code in logic. The starter files under Setup are that set: a laboratory downloads them, checks and edits each row, and imports them. Table II sets its limits per parameter, so a holding rule covers every analyte of its method and matrix or names one analyte, whose rule then replaces the method-wide rule for that analyte and clock. A starter method that covers parameters with different limits carries a method-wide rule at the shortest limit and a rule for each analyte with a longer one | Table II, SW-846 |
| QC elements | Methods call for demonstration of capability, method detection limit (MDL), blanks, laboratory control samples, matrix spike and duplicate, calibration checks, surrogates, control charts and corrective action. Frequency and limits come from the method or lab SOP | 40 CFR 136.7 |
| Batches | Preparation batch and analytical batch can differ. QC frequency is defined per method, so do not hard-code "one per 20" | 136.7 |
| MDL | Determined from spiked samples and method blanks, using the greater value. Spikes run each quarter the method is used, and the MDL is recalculated at least every 13 months | 40 CFR 136 App. B Rev. 2 |
| Reporting limits | Vary per result: dilution, sample size and soil moisture all change them. Store the limit applied to each result, not only the method default | General practice |
| Calculated results | Where a result is worked out from bench readings, the readings are the original observations: keep each one exactly as read, with its unit. Carry the arithmetic at full precision and round once, at the end. How a value is rounded is the laboratory's documented choice: significant figures or decimal places, and whether a dropped 5 goes to the even digit or always up. A method can ask for different rounding by the size of the result. Keep the calculation and its version with the result so the reported value can be worked out again. A typed or imported value is never rounded | NIST SP 811 B.7.1; NIST GLP 9 (2019); USGS I-3765-85, section 8 |
| Preparation factors | The solution an instrument reads is not the sample: a measured portion was digested, extracted or diluted to a final volume first. Take the reading back to the sample with the amounts actually prepared (final volume over the volume or weight prepared) and any further dilution, and take the reporting limit with it by the same factors. The amounts are readings: keep them with the result | EPA 200.7 Rev. 4.4, sections 12.3 and 12.5; SW-846 6010D Rev. 5, section 12.1. For the reporting limit: general practice (the EPA CLP SOM01.2 equations, 2007, adjust a contract quantitation limit the same way) |
| Dry weight and percent solids | A soil result is on the basis of the portion as weighed. Where the portion is weighed as received and a dry-weight result is wanted, divide by percent solids ÷ 100. Where the method dries the portion before weighing, the result is already dry weight and nothing is divided, so the correction is never inferred from the basis alone. Percent solids (dry ÷ wet × 100) and percent moisture ((wet − dry) ÷ wet × 100) are shares of the sample as received. A dry-weight result says which percent solids it was worked with, and where that came from | SW-846 6010D Rev. 5, sections 11.5 and 12.2; EPA 200.7 Rev. 4.4, sections 11.3, 12.5 and 12.6; EPA CLP SOM01.2 equations (2007), Exhibit D, Low/Medium Volatiles, EQ. 6 and 8a |
| Titration worksheets | Retain initial and endpoint burette readings, sample aliquot volume and actual standardized titrant normality. Alkalinity to pH 4.5 and EDTA hardness use titrant volume × normality × 50,000 ÷ sample mL, in mg/L as CaCO3. Low alkalinity uses (2B − C) instead of titrant volume, with B and C the cumulative volumes to the two endpoints. Keep both endpoint pH readings: the first is 4.3–4.7 and the second is 0.3 lower. These are explicit calculations selected for a test; neither titrant strength nor the choice of procedure is inferred | EPA 310.1 sections 6–7 and EPA 130.2 section 8, historical public method texts read 2026-11-15; the application boundary is a product choice, not a current regulatory-approval claim |
| BOD bottle worksheets | Retain each bottle’s identity, initial and final dissolved oxygen, sample aliquot and filled bottle volumes, entered seed correction in mg/L, its source, and the analyst’s inclusion or exclusion with a reason. Calculate each concentration as (initial DO − final DO − seed correction) × bottle volume ÷ sample volume, then take the mean of the selected bottles and apply the common preliminary dilution. Round only the final mean. The laboratory chooses usable bottles and supplies its reporting limit; the worksheet does not establish that a method or QC requirement was met | Washington Ecology publication 91e20, BOD item 24 (historical equation); Wisconsin DNR BOD Seed and Method Details (public guidance), read 2026-11-15. Equal bottle weighting and the 12-bottle bound are product choices |
| Records | Monitoring records must show sample date, place and time, who sampled, analysis dates, who analyzed, method and result. Keep at least 3 years for NPDES. Make retention a lab setting because states and accreditors may require longer | 40 CFR 122.41(j) |
| Certified scope | Drinking water compliance samples count only if a lab certified by EPA or the state analyzed them. Some field measurements are exempt. A register of which methods, analytes and matrices fall in the lab's certified or accredited scope per program, with marking of results outside it, is milestone 4 work with the deferred drinking water and program support. Until then the product holds no scope register, performs no scope check and makes no certification or accreditation claim | 40 CFR 141.28; EPA 815-R-05-004 |
| Laboratory identity on reports | A report names the laboratory and gives its address. A certificate or laboratory ID is not required on every report. Where one is printed it reads as a claim of that certification: it should be the certificate valid when the report is issued, and results outside its scope must be identified. The product prints the address, contact details, logo and certificates the laboratory recorded, leaves a certificate off a report issued outside its recorded validity and tells the preparer, and prints a statement that results were not checked against any scope unless the laboratory supplies its own. The laboratory's own logo is presentation only. Accreditation body symbols are the laboratory's to use under that body's rules | NY ELAP quality systems checklist items 5133 (2023-11-22) and Certification Manual Item 132 (2024-02-12); TCEQ accreditation page (2026-09-18); 25 Pa. Code 252.401(j) and 252.705, read as a 2015 draft and a summary. Other states not reviewed |
| Amended reports | A change after issue must be clearly identified with reason. An amendment is a further document that names the original, and a replacement names what it replaces | ISO/IEC 17025:2017 7.8.8, via European Accreditation interpretation |
| Electronic records | Electronic records are acceptable if secured with an audit trail and access control | TNI interpretation, via secondary summary |
| Chain of custody | Sign at each transfer, seal containers, record seal condition and discrepancies at receipt, compare labels, COC and sample appearance, check field preservation. Practice from EPA program guidance, details vary by program and client | EPA guidance (CLP, Region 4, MARLAP) |

### Intake master data

What the intake records carry, and why. Reviewed 2026-11-10 against public sources; US practice, no single mandated form.

| Record | Carries | Basis |
|---|---|---|
| Client | Account code, primary contact, mailing address, separate invoice-to contact and address, payment terms, purchase-order-required flag, report instructions | Commercial chain-of-custody forms keep report-to and invoice-to as separate blocks ([Pace COC instructions](https://www.pacelabs.com/chain-of-custody-forms/instructions-general/)). Payment terms and the PO flag are general practice, not verified |
| Project | Number, regulatory program, permit or program ID, site and state of sample origin, standing PO, laboratory project manager, standing instructions | Same forms ask for project, site or facility, state of origin and regulatory program |
| Chain of custody | PO, standard or rush turnaround, delivery method, carrier and tracking, cooler count, special instructions | Carrier and airbill belong on the custody record ([CLP Sampler's Guide 2020](https://www.epa.gov/sites/default/files/2021-03/documents/samplers_guide_clp_guidance_for_field_samplers_november_2020.pdf)) |
| Sample | Field designation (field sample, duplicate, field, trip or equipment blank), source within the matrix (for example groundwater, effluent, sediment), remarks, coolant on arrival | Field QC types and matrix detail follow public EDD valid-value lists ([EPA Region 2 EDD valid values, 2019](https://19january2021snapshot.epa.gov/sites/static/files/2019-05/documents/r2validvaluesreferencemanual_region2_may_2019.pdf)); ice on arrival is part of the 40 CFR 136.3 Table II cooling footnotes |
| Container | Type, preservative and condition chosen from starter lists | Container and preservative vocabulary of 40 CFR 136.3 Table II and common COC forms |

Rules that follow:

- The analytical matrix stays water, wastewater or soil because tests, limits and holding rules are defined per matrix. The sample source is a descriptor inside it and must belong to it.
- Field QC designation describes what the client sent. Laboratory QC samples stay in analytical batches.
- Pick-lists for observations and containers are starter vocabularies with an "Other" entry, stored as text. They are not method requirements, and values recorded before a list existed stay valid. A lab-managed bottle catalog is a later option, not built.
- Informational fields (payment terms, PO required, report instructions, delivery method) trigger no automatic invoicing, emailing or blocking.
- New reports print the client mailing address and, when recorded, the sample source, field designation and coolant. Issued reports never change. New reports also print the laboratory's address, contact details, logo and marked certificates from the laboratory profile (see Laboratory identity on reports above).
- Not recorded as structured data yet: field measurements, soil depth, field-filtered flag, QC deliverable level, quote link on the chain of custody.

### Why this shapes the design

- **Time precision matters.** Several limits are measured in minutes or hours. Dates alone are not enough.
- **Reported data is a snapshot.** Limits, units, method version and qualifiers change over time. A report must always be reproducible exactly as issued.
- **Independent review is a control.** Nobody reviews their own results, and authorization is explicit and audited. Beyond that, who may review or authorize follows the lab's own roles.
- **Never overwrite.** Corrections add a record with a reason. History is the product's main trust feature.

## Milestones

| Milestone | Scope | Exit condition |
|---|---|---|
| 1. Core journey | The 13 acceptance outcomes above: configuration of methods, analytes, units, rules and limits; one worked chemistry method set for water and soil; manual and file result entry; PDF report; read-only client view; audit | Synthetic demo completes receipt to amended report with no manual database edits |
| 2. Throughput and delivery | Quotes and price lists, client-submitted COC and pre-login, bottle and label printing, two configured instrument-file layouts (long CSV and wide TSV, each mapped explicitly with preview and provenance), control charts, MDL study records (dated observations, calculation and independent adoption, not quarterly-use coverage tracking), one exploratory electronic data deliverable (EDD) export, scheduling by due date, subcontracted tests | Real lab can run a week of routine work |
| 3. Quality system and operations | Standards, reagents and inventory with expiry, instrument calibration and maintenance, analyst training and demonstrations of capability, nonconformance and corrective action, sample storage and disposal, retention policies, laboratory announcements for staff, client users and the sign-in page | Lab can show equipment and personnel records beside sample data |
| 4. Programs and breadth | Program certification or accreditation scope register and marking (drinking water first), more deliverable formats (federal and state), permit-reporting exports, additional disciplines such as microbiology, multi-lab | Chosen later by the project |

Status, 2026-11-15: milestones 1 to 3 are implemented, with the limits in [Workflow rules and limits](workflows.md). What comes after them is the public [roadmap](../website/about/roadmap/index.html), published at <https://openbga.github.io/environmental-lims/roadmap/>. It carries no dates and sorts the remaining work into Next (what a laboratory needs for daily use: a stable release, instrument data, invoicing, notifications and deliverables), Later (quality system records, management view and milestone 4's programs and disciplines) and Exploring. From its first Next area these are implemented (2026-11-12): the laboratory's address, logo and certificate numbers on reports; import of clients, projects, methods, tests, limits and rules from spreadsheets saved as CSV; starter files for common methods, each rule naming its public source; and acceptance scripts a laboratory runs on its own installation, which record what passed on synthetic data and make no validation claim. Holding-time limits per analyte, added to that area when the starter files showed the gap, are implemented (2026-11-14): a rule may name one analyte of its method, and the starter files give EPA 300.0 and EPA 365.1 their Table II limits per analyte. Versioned releases remain. From the second Next area, calculated results at the bench, these are implemented (2026-11-14): versioned calculations whose readings, version and rounding are kept with each result so the reported value can be worked out again; rounding rules set by the laboratory per test; a dilution factor applied to the result and the reporting limit; and a worksheet for solids by weight. A second increment the same day added preparation factors applied to results and reporting limits (by volume and by weight), percent solids and percent moisture by weight, and a dry-weight correction for soil that keeps the percent solids on the worksheet. A third increment (2026-11-15) added alkalinity titrations (pH 4.5 and the low-alkalinity two-endpoint equation) and total hardness by EDTA, with burette readings and actual titrant normality retained. A fourth increment (2026-11-15) added BOD bottle worksheets: initial and final dissolved oxygen, actual sample and bottle volumes, entered seed correction with its source, and explicit bottle selection. The reported value is the equally weighted mean of selected bottle concentrations, multiplied by the common preliminary dilution and rounded once. Seed-control calculations, automatic bottle eligibility, CBOD-specific records and other titrations are outside the implemented set. The roadmap is the one list of planned work: change it in the same increment when an area ships or the order changes, and keep this document for the requirements and domain rules of work being built. Its order rests on the public rules cited here and on its own page, on public laboratory system requirements and on the gaps recorded in these documents, reviewed on that date.

For the first stable release, CI rehearses the upgrade of a synthetic installation from
`main` (added 2026-11-18). The candidate must preserve custody and audit events, result
revisions and worksheet readings, the prepared invoice draft and its charges, and issued
report snapshots and files. A client must be able to download the same original and amended
PDFs after the upgrade. Since 2026-11-19, the successful rehearsal provides upgrade notes for
the two commits it tested. Deployers can inspect the migrations, changes to locked package
versions and recovery instructions for that candidate. The notes include new or revised
technical guidance from the candidate changelog, with links to the tested revision. When
the baseline predates changelog entries, all candidate technical notes are available for
reference. Publishing the first tagged release and its maintainer-reviewed notes remains on
the roadmap.

Instrument-file support uses explicit long/wide CSV/TSV mappings and, from 2026-11-15,
plain XLSX worksheets. An analyst names the visible worksheet for each upload and previews
its values against the batch before confirming. Text and stored numeric decimal text are
preserved without binary-float conversion; display formatting does not supply significant
digits, units or leading zeros. Digits already lost when a spreadsheet was saved cannot be
recovered. Identifiers and analysis timestamps must be text. Formulas, cached formula results,
merged cells, error cells and typed dates in the selected sheet are refused; hidden rows and
columns are included. Macro-enabled, encrypted and external-link workbooks are outside this
bounded input format. The original file remains the laboratory's record; the application keeps
its digest, selected worksheet, row/column provenance and mapping snapshot with the import.
Confirmation rechecks the source and current records atomically. Resource bounds (1 MiB upload,
20 MiB expanded workbook, 200 ZIP members, 10,000 worksheet rows, 256 columns, 100,000 cells and
2,000 results/blank mapped cells) are product choices for synchronous processing. Setup imports
remain CSV-only. Qualifying a particular instrument/software export still requires representative
files; these mappings are not vendor-qualified adapters.

Analysts can export a generic CSV run list of the current analytical batch's assigned tests and
recorded QC samples for instrument setup. It includes sample disposition and disposal state,
current configured units and limits, and the latest result's explicitly recorded preparation
identity only when that revision belongs to this batch. It never chooses an aliquot, supplies
missing QC or calibration standards, assigns injection order, or sends commands to an instrument.
Analysts can also download a two-row CSV template, give the columns their instrument’s headings,
and move or remove columns. They keep the template locally and can reuse it on another batch.
The same CSV template can produce a comma-separated CSV or a tab-separated TSV. Both
use UTF-8 and double quotes to enclose fields containing a delimiter or quote. An embedded
quote is doubled. Both formats use the same current batch values and row order as the
standard download. The laboratory checks the layout and verifies the intended run.
Staff can export the current inventory even after a batch closes. Synchronous downloads have a product limit
of 2,000 rows; formula-like text is refused without rewriting identifiers. No vendor
qualification or analytical acceptance is implied. Vendor-specific run formats remain roadmap work.

Run calibration evidence, added 2026-11-15, records the instrument-reported fit/equation,
standard references and exact concentration/response text for one test in an analytical batch.
Each record freezes its method, analyte, units and recorded equipment identity. An analyst may
explicitly choose that curve and one calibration-verification QC observation on a new result
revision through the single-result form; no curve is inferred for grid, file or BOD-bottle entries.
Corrections add distinct records and result revisions. Selected evidence is retained in the result
and new report snapshots; issued versions remain unchanged. A superseded selected verification
requires a new result revision, and people who recorded the selected curve or check cannot review
or release that result. Existing configured QC checks still apply.

Staff can compare the recorded standards with a linear or quadratic fit on the calibration
detail page. Both fit response against concentration with a free intercept. Staff choose equal
weights, inverse concentration (1/x) or inverse concentration squared (1/x²) according to their
method and SOP. Each recorded point contributes separately, including replicates. To solve the
equations, a line needs two distinct concentrations and a quadratic needs three. Both inverse
options require positive concentrations throughout. Existing links that specify only the
weighting open the linear fit.

The worksheet shows the coefficients, the predicted response and the signed residual
(observed minus predicted) for each standard. For a quadratic it also reports the direction of
response across the recorded concentration range, including a turn inside the range or a
horizontal tangent at an endpoint. The worksheet calculates from exact values and rounds only
for display, to at most 12 significant digits. The instrument-reported model remains the
evidence linked to result revisions.

For linear and quadratic fits with a fitted intercept, staff can see the coefficient of
determination (R²). It measures the fraction of variation in the observed responses explained
by the fit. The worksheet calculates 1 − (weighted residual sum of squares / weighted total
sum of squares). The total is measured around the weighted mean response, with each
recorded replicate included. When all observed responses are equal,
the total is zero and R² is undefined.
[Weighted, centered total sum of squares and R²](https://www.statsmodels.org/stable/_modules/statsmodels/regression/linear_model.html#RegressionResults).

For an unweighted linear fit with a fitted intercept, the worksheet also reports the signed
Pearson correlation coefficient (r) between recorded concentration and observed response.
Each recorded standard, including a replicate, contributes once. A negative value indicates
that response tends to decrease as concentration increases. A value of zero indicates no
linear association. Constant responses leave the correlation undefined. Staff interpret r under
their method and SOP. US EPA Method 8000D discusses this use of r for unweighted linear
regression with both coefficients fitted. [US EPA SW-846 Method 8000D Rev. 5, March 2018,
§11.5.2.2, checked 2026-11-21](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf#page=37).

US EPA Method 8000D discusses R² for linear and nonlinear calibrations. Staff interpret it
alongside the residuals and their method’s acceptance criteria.
[US EPA SW-846 Method 8000D Rev. 5, March 2018, §§11.5.2.2 and 11.5.3.1, checked 2026-11-20](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf#page=37).

For US methods, EPA SW-846 6010D Rev. 5 (July 2018), section 10.7, recommends inverse weighting
for multipoint ICP-OES calibration. EPA SW-846 8000D Rev. 5 (March 2018), section 11.5.2,
discusses weighting for linear regression in chromatography and excludes zero-concentration
points for that procedure. Section 11.5.3 recommends at least six levels for a quadratic and
advises checking that the slope keeps the same sign throughout the calibration range and is
nowhere zero. Staff apply their method's requirements for levels, model selection and acceptance.
The worksheet is for inspection. [US EPA Method 8000D](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P1016J8Z.txt)
and [US EPA Method 6010D](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P1017DDQ.txt), checked 2026-11-19.

The **Linear through origin** option fixes the intercept at zero before fitting the slope.
It minimizes the sum of squared response residuals, using every recorded standard and replicate.
Staff can inspect the slope and the predicted response and signed residual at each measured level.
This comparison requires at least two distinct positive concentrations. That minimum is a
worksheet choice. US EPA SW-846 Method 8000D Rev. 5 (March 2018), section 11.5.2.1, describes
using an unweighted fit through the origin where appropriate and excludes zero-concentration
calibration points. The worksheet accepts equal weights and adds no point at the origin.
For this method, the lowest analyzed standard still sets the lower end of the demonstrated
calibration range. The laboratory determines whether the model suits its procedure.
[US EPA Method 8000D, checked 2026-11-20](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf).

For external standards, staff can also compare the instrument response per unit of recorded
concentration at each point. Each standard, including a replicate, contributes equally to the
mean calibration factor. The predicted response is the mean factor times the standard
concentration. The worksheet shows the sample standard deviation of the factors in the same
units and, for a positive mean, the relative standard deviation as a percentage. A zero or
negative mean leaves that percentage unavailable. The worksheet requires two distinct positive
concentrations to compare levels. The laboratory determines how many levels its method requires.

US EPA SW-846 Method 8000D Rev. 5 (March 2018), sections 11.4.2 and 11.5.1, allows concentration
as the denominator for an external-standard factor and describes how to evaluate the variation
among factors. Section 11.5.1 recommends at least five calibration levels for this model. The
worksheet uses the recorded concentration units throughout. Staff apply the acceptance criteria
in their method and SOP.
[US EPA Method 8000D, checked 2026-11-20](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf).

Staff can use the observed response of each calibration standard to back-calculate its
concentration for either linear fit or the average external-standard factor. The worksheet
uses the unrounded slope and intercept, or the unrounded mean factor. A zero slope or factor
makes the inverse unavailable. A zero recorded concentration can still have a back-calculated
concentration, but its percent error is undefined. Negative and out-of-range estimates remain
visible so staff can inspect the fit at the ends of the range.

For a quadratic fit, the worksheet gives a back-calculated concentration only within the
range of recorded standards. The fitted curve must increase or decrease across that range,
and the observed response must lie between the two fitted endpoint responses, inclusive.
This gives staff one possible concentration. A curve with a horizontal tangent at an endpoint
can still give a unique answer. A curve that turns inside the range or has constant response
makes back-calculation unavailable for every row. A quadratic that reduces to a straight line
uses the same range restriction. Percent error uses the unrounded concentration.

The closed-range check is a product choice for inspecting recorded standards. A laboratory
using US EPA Method 8000D should also consider the guidance in section 11.5.3.2 to check
for zero slope anywhere in the calibration range.
[US EPA Method 8000D Rev. 5, March 2018, checked 2026-11-20](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf#page=39).

US EPA SW-846 Method 8000D Rev. 5 (March 2018), section 11.5.4.1, describes comparing
recalculated and known calibration levels. The worksheet displays the magnitude of their
difference as a percentage of the recorded concentration. Staff may enter the maximum absolute
percent error from their procedure and, if applicable, a separate maximum for the lowest
recorded concentration. That second maximum applies to every replicate at that concentration.
The worksheet compares each unrounded error with its applicable maximum. Equality is within
the maximum. A zero concentration or unavailable back-calculation leaves the comparison
unavailable for that standard. The limits apply to the current worksheet view. The laboratory
makes the acceptance decision under its own method and SOP.
[US EPA Method 8000D Rev. 5, March 2018, §11.5.4.1, checked 2026-11-21](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf#page=39).

For a free-intercept linear fit, a quadratic fit or an average external-standard factor, staff
can inspect the relative standard error across the recorded calibration points. US EPA SW-846
Method 8000D Rev. 5 (March 2018), section 11.5.4.2, defines it as a percentage. The worksheet sums
the squared relative concentration errors, divides by the number of points minus the number
of fitted terms, takes the square root and multiplies by 100. The average factor has one fitted term,
the line has two and the quadratic has three, even when its quadratic coefficient is zero. Each
replicate counts as a point. The selected weights affect the fitted coefficients. Each point
contributes once to the error calculation using the unrounded fit. RSE requires nonzero
recorded concentrations and positive degrees of freedom. For a line, its slope must be
nonzero, and for an average-factor fit, its mean factor must be nonzero.

A quadratic needs at least four recorded points across at least three distinct concentrations
for this statistic. Every standard must have a nonzero recorded concentration and a unique
back-calculated concentration in the recorded range. If any standard lacks either, staff can
still inspect individual rows and see why the overall statistic is unavailable. Staff apply
their method's acceptance criteria. [US EPA Method 8000D Rev. 5, March 2018, §11.5.4.2, checked 2026-11-22](https://www.epa.gov/sites/default/files/2015-12/documents/8000d.pdf#page=40).

For an analytical batch, an analyst can transcribe the instrument's actual injection sequence
and name the source record. Each run has its own reference within the batch. A position has
the instrument label and the analyst's classification as a sample, verification, standard,
blank or other injection. Repeated labels occupy separate positions. A transcription holds
1–200 consecutive positions starting at one, a product limit for review in one worksheet.
Closing a batch freezes its membership. Analysts can still record sequence evidence or correct
a transcription with a reason. Each version retains its entries, author, source and timestamp.

Staff can find the nearest earlier and later injections marked as verifications for each
sample in that version. Staff can also count sample injections between consecutive verifications
and in any positions before the first or after the last verification. Repeated sample
injections count individually. Consecutive verifications have zero samples between them.
A run without a verification shows one interval with both ends missing. Staff can enter a
maximum sample count for comparison. The worksheet shows missing ends independently of
the count and saves no decision. The laboratory checks the original run, the applicable
analyte, the verification results and the method's requirements. Instrument
labels alone do not establish a link to a sample, a QC observation or a result revision.
For example, US EPA SW-846 Method 6010D, Rev. 5 (July 2018), section 10.8.5, specifies checks
during the run and at its end. The laboratory supplies the applicable frequency and
acceptance criteria.
[US EPA Method 6010D, checked 2026-11-20](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P1017DDQ.txt).

Method-specific verification bracketing remains with the laboratory. EPA 200.7 Rev. 4.4
(1994), sections 9.3.4 and 11.4.4, and SW-846 6010D, sections 10.7–10.8, describe different
verification arrangements. Each calibration record can hold up to 50 standard points, and
each result revision can link to one verification. These are product bounds. The calibration
record accepts negative instrument responses and retains the supplied standard references. [US EPA Method 200.7 Rev. 4.4, checked 2026-11-15](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P10177CN.txt).

Analysts share a dashboard queue for results returned from technical review. The time waiting
starts when the reviewer returns the current revision. Analysts can open the oldest return
first, read the reviewer's reason and go directly to the result. Saving a correction clears
that reminder and keeps the earlier decision in the result history.

Account-hold notices, added 2026-11-16, are the first invoicing increment. Project managers
and administrators can mark a client on hold and record a staff note. Client edits retain the
actor, reason and old/new values and reject stale changes. Staff see the current notice while
receiving samples and on the report-release preview. It is informational: the laboratory decides
what to do, and neither receipt nor release is automatically blocked. The flag is distinct from
client inactivity, sample disposition, QC holds and explicit release authority. Notes remain
internal and are excluded from client pages, report snapshots, PDFs and deliverables. Clearing
the flag retains the note for staff unless explicitly changed. The 500-character note bound and
notice-only behavior are product choices, not billing or regulatory requirements. There is no
balance calculation, invoice, automatic payment action or accounting integration in this increment.

Draft invoices, added 2026-11-16, let project managers and administrators prepare charges from
the current requested tests on a received order and an explicitly selected active price list
or accepted quote version. Every test needs a recorded price. Amounts use exact USD decimals,
and each draft retains the source test identities, sample dispositions, selected pricing source
and billing details used to
prepare it. The order PO takes precedence over the project's standing PO; a blank invoice
address uses the client's mailing address. Blank invoice contact/email use the primary contact/email.
Later source changes do not update a draft.
Staff review the included work and may edit the billing details, descriptions, quantities and
prices with a reason. Inclusion does not establish that a test is chargeable, completed or
analytically accepted. One active draft per order and a 200-line limit are product bounds.
Creation, edits and cancellation retain attributed history and reject stale submissions;
cancelled drafts remain available internally. Project managers and administrators handle
issuance and manual delivery. Payments stay in the accounting system. Staff can export the charges after issuance.

Accepted-quote pricing, added 2026-11-16, is an explicit alternative to a price list. The quote
must be accepted for this client and either this project or no particular project, and the
selected issued version must be the accepted version. Its full set of test identities and
quantities must exactly match the received order, with one quote line per test definition.
Missing, extra and duplicate test lines are refused; nothing falls back to a list or
is silently omitted. Each received requested test copies its quoted description and exact unit
price at quantity one. The draft retains the accepted version, its number/title/acceptance
snapshot and each source quote line through later edits and cancellation. Billing details and
PO still come from client/project/order records; quote terms are not copied. The quote's
valid-until date governs acceptance, not use of an already accepted version.

Quote fees and discounts, added 2026-11-16, transfer once per accepted quote line after the
received-test lines. Their kind, description, positive whole-number quantity, signed USD unit
price and source quote line are retained; they have no sample or requested-test identity.
Tests and fees use nonnegative prices; discounts use zero or negative prices, in whole cents.
The combined draft is limited to 200 lines and a nonnegative total. These sign and size rules
are product bounds, not tax or accounting requirements. Reasoned edits can change descriptions,
quantities and prices but preserve every line, its kind and provenance. Discounts are fixed
amounts multiplied by quantity; percentage discounts are not supported.
Matching and transfer are per order and do not allocate a quote across multiple orders.

Manually added fees and discounts, added 2026-11-16, extend an active draft without changing
its original received-test or quote snapshot. A project manager or administrator chooses the
kind, description, positive whole-number quantity and signed whole-cent USD price and gives a
reason. Each added line has a distinct manual origin, without a fabricated sample, test or quote
link. The same sign rules, combined 200-line bound and nonnegative total apply. Adding a line
rechecks the locked draft revision and records its actor, reason and before/after state together.
Every line stays in the draft with its kind and origin fixed; staff correct an unwanted amount
to zero with a reason or cancel the draft. Cancelled drafts cannot receive more lines. These are
product choices for retained internal billing preparation, not tax or accounting requirements.

Draft due dates, added 2026-11-17, are optional calendar dates entered by project managers or
administrators while editing a draft. Existing and new drafts start without one. Setting,
changing or clearing a date requires a reason and preserves the before/after values with the
actor and revision; stale and cancelled drafts refuse changes. An entered date takes precedence at issuance. Staff
can enter a past date to record an earlier agreement. The directory supports inclusive date-range
filters and date sorting. A date is preparation for billing, not evidence of an issued invoice,
an outstanding balance, payment or delinquency.

At issuance, a draft with no entered due date and Net 15, 30, 45 or 60 days in its saved billing
details offers the issuer a proposed date. It counts calendar days from the invoice date in the
deployment time zone, including weekends and holidays. The issuer can accept the displayed
date or leave the invoice undated. If the issue date changes before confirmation, the issuer
must review the new proposal before accepting it. An accepted date is retained on the billing
record, in the issuance history and on the original invoice PDF. Due on receipt, prepaid and
unspecified terms have no fixed interval from issue and offer no proposal.

Net terms measured from the invoice date in calendar days follow established invoicing
practice. Offering the date for staff confirmation is a product choice. [Customer payment
terms, Microsoft Learn, checked 2026-11-17](https://learn.microsoft.com/en-us/dynamics365/finance/general-ledger/tasks/establish-customer-payment-terms).

Manually recorded tax lines, added 2026-11-17, retain an externally determined tax amount
on an internal draft. Staff choose Tax, enter the nonnegative whole-cent USD amount at quantity
one, describe its basis or assessment source, and give a reason. Separate assessments may use
separate lines. Each tax line has a manual origin and no sample, test or quote reference. It
participates in the exact draft total, stays present through corrections and cancellation, and
uses the same authorization, revision and audit gates as other manual lines. An unwanted amount
is corrected to zero with a reason; a missing tax line makes no tax determination.
The application does not determine taxability, jurisdiction, rates, exemptions or a taxable
base, and changing other charges never recalculates a recorded tax amount. Staff review the
assessment whenever charges change. These are recordkeeping product choices, not tax advice or
a qualification of the amount entered. Automatic tax calculation remains separate work.

These are deliberately bounded product choices for whole-order draft preparation, not a
contract interpretation or billing qualification. Quote quantities are matched per order;
no cumulative consumption, reservation, repeated-use prohibition or ledger is implied. Staff
review applicability before creating a draft and retain responsibility for reasoned later edits.
Accepted quotes becoming invoice sources are an established software pattern
([Stripe quote acceptance](https://docs.stripe.com/api/quotes/accept), checked 2026-11-16).

Invoice issuance, added 2026-11-17, lets a project manager or administrator finalize a
reviewed draft for manual delivery. The invoice has its own number. It freezes the issuing
laboratory, billing details, due date if present, itemized USD charges and total.
The document labels tax amounts as Tax and keeps their assessment references internal.
Every download returns the PDF stored at issue, including after later source edits or a
void. An order can have one active draft or issued invoice. A reasoned void retains the
issuer, original document and history, and permits a new draft from the order's current
sources. Staff keep responsibility for sending invoices, handling corrections with clients
and recording payments in their ledger. The application sends no email and exposes no
invoice through the portal.

Finalization and voiding follow an established invoice workflow. The roles, identifiers,
document contents and replacement procedure above are product choices. [Invoice lifecycle,
checked 2026-11-17](https://docs.stripe.com/invoicing/overview).

An issued invoice can supply a CSV for manual accounting import (added 2026-11-18).
Project managers and administrators can download one row per issued line. Each row identifies
the invoice and line position, client and client account code, purchase order, source order
and payment due date when supplied. Discounts keep their signed USD price and amount. Tax
lines carry the issued display description. The invoice number and line position let staff
identify repeat downloads. Only an invoice still in Issued status can be exported. Staff map
the file to their accounting system, check its treatment of discounts and tax, and reconcile
any subsequent void there.

One row per charge with a repeated invoice number supports manual column mapping. The receiving
system determines the required fields and which charges it accepts. [US invoice import guidance,
checked 2026-11-18](https://quickbooks.intuit.com/learn-support/en-us/help-article/import-export-data-files/import-multiple-invoices/L7E9Xrd8l_US_en_US).

A client can read the current issued result table in the portal and download any issued version
of its own report as CSV or as an Excel workbook (.xlsx). In either standard download, each row
identifies the report version and sample and carries the reported result text, unit, qualifiers
and reporting limit from that issue. Historical downloads use the original issue after an amendment.
Clients can use the same two-row CSV template in either format to select fields and set column
headings and order. They keep the template locally. Both downloads preserve issued row order.
The Excel worksheet stores values as text so sample IDs retain leading zeros and results keep
the precision and qualifiers the laboratory reported. If a selected value cannot be written
exactly in the workbook, clients can download that version as CSV. The stored PDF is the report of record.

Each issued version also has a ZIP package containing its stored PDF and standard results CSV.
Receipt staff can request a separate QC summary on a chain of custody. The release preview
identifies the selected samples, and the issue freezes that choice. For a report spanning
multiple orders, the text summary covers the analytical and preparation batch tests linked to
the selected samples. It uses the same client-scoped evidence and withholding as the issued
report. An order edit can affect a later issue. Earlier versions keep their original selection,
and versions issued before this option have a PDF and CSV package.

Clients can open the result history for a sampling location from an issued report. Within the
client’s project, the history matches the location text exactly as issued and shows the latest
issue of each report, ordered by sample collection time. If two distinct reports include the
same result, both appearances retain their report number and version. The issued values, units,
qualifiers, reporting limits, matrix, result basis and method give the client the context for
reading the series. Earlier issues remain available in the report’s version history.

From a result in that history, a client can enter an upper limit in the issued unit. The
comparison covers the same analyte, matrix, unit and reporting basis at that project location.
It shows whether an unqualified numeric result is above, at or below the supplied value using
the exact issued decimal. Non-detects and qualified results retain their issued wording, with
a reason the comparison was withheld. This includes a qualification recorded at receipt,
for the result’s preservation, or against its test in the analytical or preparation batch.
Soil results need a recorded wet or dry weight basis. The limit and the outcome belong to
the comparison view. The issued report remains the source for the method and qualification.

This conservative comparison is a product choice. For example, the US EPA’s Superfund
inorganic data review guidance distinguishes estimated quantities from non-detects. The portal
leaves interpretation of those results to the client and laboratory.
[US EPA guidance, General Table 1, November 2020, checked 2026-11-19](https://www.epa.gov/sites/default/files/2021-03/documents/nfg_for_inorganic_superfund_methods_data_review_november_2020.pdf).

Since 2026-11-19, a client preparing a discharge monitoring report can pair one concentration
from its current issued report with the corresponding sampling-day flow in million US gallons
per day. The daily load worksheet shows the issued concentration and unit, its value in mg/L
and the product of that value, the entered flow and 8.34, in lb/day. It supports water and
wastewater concentrations issued in mg/L or micrograms per liter. A non-detect, a qualification
or insufficient evidence leaves the load unavailable. The report PDF identifies the source.
The worksheet is transient. The permittee decides whether this concentration, flow and factor
apply to the discharge and how to round and report the result.

US EPA’s 2017 permit ID0020214 for Idaho illustrates this calculation in Table 1, note 1.
It has separate rules for averaging and for results below detection limits. The permittee files the report.
[US EPA, 2017 Idaho permit, page 8, checked 2026-11-19](https://www.epa.gov/sites/production/files/2017-05/documents/r10-npdes-preston-id0020214-final-permit-2017.pdf).

The monthly concentration worksheet uses the collection month on the issued result. Across the
project’s current issued reports it groups the same named location, aqueous matrix and analyte.
A day with one eligible result uses its issued concentration. For a day with several eligible
grab samples, the permittee enters the flow at the time of each grab in million US gallons per
day (MGD). The worksheet multiplies each concentration in mg/L by its flow, sums those products
and divides by the sum of flows for that day. Each sampled day then has equal weight in the
monthly mean. The maximum is the highest daily concentration. Both calculations retain exact
values, including fractions for nonterminating decimals. The worksheet links to each issued PDF.

Flows must be positive for every grab on a day that needs weighting. This product choice prevents
a zero weight from silently omitting a grab. Each grab that needs a flow must have an issued
collection time and UTC offset so the permittee can match the measurement. A repeated lab
sample ID, a composite alongside another observation on the same day, a cross-day composite,
a non-detect, a qualification or incomplete evidence withholds both figures. Flows are transient.
If the issued sources change, the permittee checks the current series and enters the flows again.

US EPA’s 2024 eastern Gulf of Mexico general permit GEG460000 illustrates this calculation
for pollutants other than bacterial indicators in Part II.E.4(a, c) and II.E.7(c). Part V.B.20
of the same permit also describes weighting daily concentrations by daily flow across the month.
The worksheet implements the equal-day calculation and groups by the local dates on issued
reports. The permittee checks the applicable averaging rule, coverage, representativeness of
each measurement, units and rounding before filing.
[US EPA, permit GEG460000, pp. 99–102](https://www.epa.gov/system/files/documents/2024-03/geg460000-2024-finalsigned.pdf#page=99)
and [pp. 158–159, checked 2026-11-19](https://www.epa.gov/system/files/documents/2024-03/geg460000-2024-finalsigned.pdf#page=159).

Deliverable and reporting targets for milestones 2 to 4. All are exploratory. A recipient accepts an EDD only against its own current specification and reference values, so no format here is claimed as accepted until tested against one named recipient's spec.

| Target | What it is | Notes |
|---|---|---|
| EQuIS-style four-file EDD | Sample, test, result and batch files zipped with a reference value file (EarthSoft lab guide, Oct 2024). Regulators publish their own variants, for example New York State DEC format v5, May 2024 | Not a generic CSV. Pick one recipient, use its spec and valid values, validate with its tools. Needs batch and QC detail, so the milestone 1 data model must keep them |
| Staged Electronic Data Deliverable (SEDD) | EPA XML format, Stage 1 results to Stage 3 raw data. Specification 5.2, March 2019 | Used in federal programs. Later, if at all |
| State formats | For example California GeoTracker EDF 1.2i | One at a time, on request |
| Discharge monitoring reports | Electronic since 2016-12-21 under 40 CFR Part 127, submitted by the permittee through the state or EPA system | The lab supplies results, usually not the filing |
| Water Quality Exchange | EPA XML or template upload | Mostly for monitoring programs, not routine lab work |

## Quality bar for implementers

The project wants clean, reusable code, standard libraries and components, a professional interface, and no overengineering. For this domain that means:

- Prefer a few well-modeled concepts (sample, container, test, batch, result, report version) over configurable generality. Add configuration only where a lab must change it: methods, analytes, units, limits, holding rules, qualifiers.
- Professional means dense, scannable, keyboard-friendly tables for receipt, batches and review, with clear status, holding-time warnings and queues per role. Chemists work in volume.
- Staff navigation shows work destinations for the account’s effective roles and explicit capabilities, combines destinations across roles, and omits empty sections. Related record links and direct pages retain their existing authorization; client portal navigation remains separate.
- Tests concentrate on the rules in the table above: holding-time computation, QC evaluation, separation of duties, report immutability and amendment lineage, audit completeness. Skip tests that restate framework behavior.
- Keep documentation to intent and constraints. Do not duplicate the code in prose.
- Every application page has contextual operator documentation, opened by a top-right icon
  in a resizable right panel. It follows page and form navigation and remembers its open/closed
  state and preferred width in the current browser for the current account. Document purpose,
  every action, tab and field (including its use and record relationships), and implicit filters,
  eligibility and other page rules. Keep this help current in the same change as the behavior.
  Operator help describes the user's workflow; source remains the implementation reference.
- Use only synthetic data. No real client, site or permit identifiers anywhere in the repository, demo or tests.

## Project decisions

| # | Decision | Status | Effect |
|---|---|---|---|
| 1 | Lab type: commercial US water, wastewater and soil lab | Confirmed | Sets the product's audience |
| 2 | Tenancy: one lab organization per deployment | Confirmed | No multi-lab isolation layer. Clients are records inside the lab |
| 3 | First-release focus: chemistry first, with drinking water compliance, microbiology and other disciplines deferred | Working default, not an exclusion | Contributors may widen scope when it is cheap and does not distort the core. Microbiology (counts, most probable number) and drinking water program reporting would change the result model |
| 4 | Client access: read-only client view in milestone 1, client-submitted COC in milestone 2 | Working default | Revisit only if client-entered orders are wanted in the first release |
| 5 | First electronic deliverable: PDF in milestone 1, one exploratory EDD in milestone 2 | Working default | Choosing a real recipient early pulls batch, QC and qualifier detail into the first data model |
| 6 | Accreditation posture: support the records NELAP and ISO/IEC 17025 labs need, no compliance claim, state rules added on request | Working default | State and accreditor change report content, retention and scope marking |
| 7 | Review independence: nobody approves a result they entered or submitted. Recording a batch's QC measurements does not by itself bar that person from approving the batch's sample results; only accepting a QC failure is refused to whoever measured it | Working default | Whether QC measurements count as a reviewer's own work is laboratory policy. Decide before real use; a stricter rule changes who can review |

## Evidence and limits

- Federal rules cited are in the eCFR, queried 2026-11-08. The latest recorded amendment to 40 CFR 136.3 is 2024-06-17. Re-check before relying on a specific table value.
- Drinking water certification follows EPA manual EPA 815-R-05-004, fifth edition, with two supplements. No newer edition was found.
- State rules differ. Texas, California, New York, Florida and other NELAP states each have their own report and record rules. This research did not review them.
- The TNI standard and ISO/IEC 17025 are sold standards. Statements here rely on public interpretations and summaries, marked as such, and are not a substitute for reading the standard.
- Certificate numbers on reports were researched 2026-11-12 for New York, Texas and Pennsylvania only (Pennsylvania through a 2015 draft and a summary of the current text). No source gives approved wording for a report that cannot mark which results a certificate covers, so the default statement is the project's own. A laboratory's phone, email and website on a report are general practice, not a cited rule. A subcontract laboratory's certificate number, which Pennsylvania asks for on the final report, is not recorded or printed.
- The starter files were read on 2026-11-12 from 40 CFR 136.3 Tables IB and II (eCFR current to 2026-11-10; section last amended 2024-06-17), EPA methods 200.7, 200.8, 245.1, 300.0, 350.1, 351.2, 353.2, 365.1, 410.4 and 180.1, and SW-846 Chapter Three (Update VI, 2018) with methods 6010D, 6020B and 7471B. A second reader then compared every value with the saved source text, and the corrections from that reading were applied. Standard Methods texts were not read: those entries carry only what the CFR says about them. The files hold no reporting limits, no soil tests (no source gives a unit) and no preparation methods, and "6 months" is entered as 180 days. Units are the ones Table IB names. The quality-control rows cover only the laboratory fortified blank and matrix recoveries and frequencies of EPA 200.7, 200.8, 245.1 and 300.0, with the method's conditions quoted beside each limit. The files are starting points: Table II is a Clean Water Act rule for aqueous samples, not a drinking water rule, and a state, permit or method can be stricter. The per-analyte holding rules added on 2026-11-14 rest on the Table II rows read on 2026-11-12, compared that day with the eCFR text current to 2026-11-12 (the eCFR lists a non-substantive reissue of the section on 2026-11-12, and a comparison of the two texts found only table markup differences; last amended 2024-06-17). That an analyte's rule replaces the method-wide rule, and that the method-wide starter rule carries the shortest limit, are the project's conventions, not a source's.
- Calculated results were researched on 2026-11-14. Rounding: NIST SP 811 (Guide to the SI) section B.7.1, read from the NIST page updated 2025-08-18, gives the half-to-even rule; NIST GLP 9 (NISTIR 6969, 2019) presents three options in common use (that rule, spreadsheet rounding, and rounding uncertainties up), says the laboratory should document which it uses and says not to round intermediate values. Both are metrology guidance, not an environmental rule: no federal source read here sets one rounding for laboratory results, so the product offers both and has no default. Solids by weight: USGS methods I-3765-85, I-1749-85 and I-3750-85 give solids in mg/L as 1,000 ÷ mL of sample × mg of residue and report whole numbers below 1,000 mg/L and three significant figures from there up; the texts were read from the National Environmental Methods Index. Standard Methods 2540 and EPA 160.2 were not read. The USGS suspended-solids method applies a correction for any loss shown by a blank, which the product does not do. That a dilution factor multiplies both the reading and the reporting limit is general practice, not verified against a source. The project's own conventions, not a source's: one calculation per test, a typed result refused where a rule is active, a calculated value below the reporting limit reported with a qualifier or as ND by the analyst, comparison of the rounded value with the limit, the choice between two roundings by the size of the value before rounding, and 50 significant digits carried before rounding.
- Preparation factors and dry weight were researched on 2026-11-14 from EPA Method 200.7, Revision 4.4 (1994), sections 11.3 and 12.3 to 12.6, and SW-846 Method 6010D, Revision 5 (July 2018), sections 11.5, 12.1 and 12.2 (both EPA files saved 2026-11-12; their equations are images and were read from the rendered pages), and from EPA's Contract Laboratory Program document "Equations utilized in conjunction with SOM01.2" (2007-08-22), Exhibit D, Low/Medium Volatiles, EQ. 6, 8a, 10 and 11 (the numbers differ in its other exhibits), read from an EPA web snapshot. The two methods differ: 6010D weighs the portion as received and divides by percent solids, 200.7 dries the portion first, so the product offers both as separate calculations for the laboratory to choose per test. EPA 200.7 section 12.4 rounds the solution concentration before the solids calculation; the product uses each reading as typed and rounds only the result, so a laboratory following that section types the rounded concentration. The CLP equations adjust a contract quantitation limit from a contract sample amount by the amount prepared, the dilution and the moisture, and EPA 200.7 section 12.3 speaks of a detection limit adjusted for the aliquot and dilutions. Neither is a rule for a laboratory's own reporting limit, so scaling that limit by the same factors stays general practice. The product takes the limit of the measured solution as a reading and scales it; it does not adjust the catalog limit from nominal amounts. The project's own conventions, not a source's: rounding the limit worked out by the test's rounding rule; requiring the dry-weight basis for a corrected result and the wet-weight basis for percent solids and moisture; typing the percent solids with its source, with no link to the sample's percent solids result; the three unit pairings offered (mg/L to mg/kg, ug/L to ug/kg, ug/L to mg/kg); refusing percent solids weights in which the dried sample weighs less than the dish or more than the sample as received; and reading the micro sign, the Greek mu and a plain u as one spelling of the same unit. Soil mechanics uses a water content that divides by the dry weight; that is a different quantity, it is not offered, and this note about it is general knowledge that was not checked against a source. Standard Methods 2540 G and the SW-846 preparation methods (3050B and others) were not read.
- Items marked "not verified" or "general practice" are common domain knowledge that should be confirmed against the method or state before they become hard rules.

The titration equations were read on 2026-11-15 in EPA Method 310.1 (issued 1971, editorial revision 1978), sections 6.2–6.3 and 7.1–7.2, and EPA Method 130.2 (editorial revisions 1978 and 1982), section 8. The low-alkalinity procedure describes concentrations below 20 mg/L and 100–200 mL portions; the worksheet does not infer suitability from the result. The pH 3.9 mineral-acid variant, low-hardness blank correction, burette refills and other titrations are outside this increment. Choosing a rounding, reporting limit, qualified method and acceptance criteria remains the laboratory’s work. The software checks the stated pH endpoint values without inventing a tolerance; it does not perform a titration or establish method qualification.

BOD arithmetic and seed-correction meaning were researched on 2026-11-15 from Washington Ecology publication 91e20, BOD checklist item 24, and Wisconsin DNR’s public BOD Seed and Method Details guidance. The seed correction is the oxygen demand attributable to added seed in that particular bottle, in mg/L; it is supplied with a source reference, not estimated by the application. Raw seed-control readings and their selection remain in the referenced laboratory record. No dilution-water blank correction is applied. The 2023 Wisconsin checklist discusses bottle depletion, residual oxygen and QC criteria, but this increment does not encode those thresholds or decide method eligibility. It requires nonnegative measured DO and correction, positive volumes, sample volume no greater than filled volume, an explicit selection and a reason for every excluded bottle. A selected bottle cannot have negative seed-corrected depletion; excluded bottles can retain that observation. These are product entry rules, not a claim of analytical acceptance. At least one complete selected bottle is required; all-bottles-fail bounds and exception reporting are outside this calculation. Setup/readout times and incubator records are not added as structured bottle fields. The result’s analysis start records setup time; further details belong in comments and the laboratory’s records. The US federal 40 CFR 136.3 Table IB and footnote 12 distinguish BOD5 from CBOD5 and their inhibitor treatment. The worksheet does not infer inhibition, validate incubation or claim current method approval. EPA 405.1 full text was not read and is not used as a current approval source.

### Primary sources

| Claim area | Source |
|---|---|
| Holding times, preservation, footnotes 4, 18, 22 | [40 CFR 136.3](https://www.ecfr.gov/current/title-40/part-136/section-136.3) |
| QC elements | [40 CFR 136.7](https://www.ecfr.gov/current/title-40/part-136/section-136.7) |
| Starter methods, analytes and QC recoveries | [EPA approved Clean Water Act chemical test methods](https://www.epa.gov/cwa-methods/approved-cwa-chemical-test-methods), [SW-846 compendium](https://www.epa.gov/hw-sw846/sw-846-compendium) |
| Rounding of calculated values | [NIST SP 811, Appendix B, section B.7](https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors), [NIST GLP 9, 2019](https://www.nist.gov/system/files/documents/2019/05/14/glp-9-rounding-20190506.pdf) |
| Solids by weight | USGS methods (Techniques of Water-Resources Investigations, book 5, chapter A1) in the National Environmental Methods Index: [I-3765-85, suspended](https://www.nemi.gov/methods/method_pdf/5495/), [I-1749-85, dissolved](https://www.nemi.gov/methods/method_pdf/5473/), [I-3750-85, total](https://www.nemi.gov/methods/method_pdf/5493/) |
| Preparation factors, dry-weight results and percent solids | [EPA Method 200.7, Revision 4.4 (1994)](https://www.epa.gov/sites/default/files/2015-08/documents/method_200-7_rev_4-4_1994.pdf), [SW-846 Method 6010D, Revision 5 (2018)](https://www.epa.gov/sites/default/files/2015-12/documents/6010d.pdf) |
| Percent moisture and adjusted quantitation limits | [EPA Contract Laboratory Program, Equations utilized in conjunction with SOM01.2, 2007-08-22](https://19january2021snapshot.epa.gov/sites/static/files/2015-03/documents/som12_eq_guidance_08-22-07.pdf) |
| BOD bottle arithmetic and seed correction | [Washington Ecology publication 91e20](https://apps.ecology.wa.gov/publications/documents/91e20.pdf), [Wisconsin DNR BOD Seed](https://dnr.wisconsin.gov/topic/labCert/BODSeeding.html), [Wisconsin DNR Method Details](https://dnr.wisconsin.gov/topic/labCert/BODMethod.html), [Wisconsin BOD/CBOD checklist, 2023](https://dnr.wisconsin.gov/sites/default/files/topic/LabCert/BOD_Final_2023-11-23.pdf) |
| Titration equations | [EPA 310.1, archived method text](https://semspub.epa.gov/work/03/160260.pdf), [EPA 130.2, method text at NEMI](https://www.nemi.gov/methods/method_pdf/4684/) |
| MDL procedure | [40 CFR 136 Appendix B](https://www.ecfr.gov/current/title-40/part-136/appendix-Appendix%20B%20to%20Part%20136), [EPA MDL FAQ](https://www.epa.gov/cwa-methods/method-detection-limit-frequent-questions), [2017 rule](https://www.federalregister.gov/documents/2017/08/28/2017-17271/clean-water-act-methods-update-rule-for-the-analysis-of-effluent) |
| NPDES monitoring records and retention | [40 CFR 122.41(j)](https://www.ecfr.gov/current/title-40/part-122/section-122.41) |
| NPDES electronic reporting dates | [40 CFR 127.16](https://www.ecfr.gov/current/title-40/part-127/subpart-B/section-127.16) |
| Certified labs for drinking water | [40 CFR 141.28](https://www.ecfr.gov/current/title-40/part-141/section-141.28), [EPA certification manual page](https://www.epa.gov/dwlabcert/laboratory-certification-manual-drinking-water) |
| Amended reports | [European Accreditation interpretation of ISO/IEC 17025 7.8.8](https://european-accreditation.org/sp_accordion_faqs/45-2-question-on-amendments-to-test-reports-iso-iec-17025-clause-7-8-8-1/) |
| NELAP and TNI recognition | [TNI 2023 status report](https://nelac-institute.org/docs/comm/advocacy/Correspondence/State%20of%20National%20Accreditation%20%282023%29.pdf) |
| Chain of custody and receipt | [CLP Sampler's Guide 2020](https://www.epa.gov/sites/default/files/2021-03/documents/samplers_guide_clp_guidance_for_field_samplers_november_2020.pdf), [Region 4 sample management](https://www.epa.gov/sites/default/files/2015-06/documents/Sample-and-Evidence-Management.pdf), [MARLAP ch. 11](https://www.epa.gov/sites/default/files/2015-05/documents/402-b-04-001b-11-final.pdf) |
| Laboratory identity and certificate numbers on reports | [NY ELAP quality systems checklist, 2023-11-22](https://www.wadsworth.org/sites/default/files/WebDoc/NYSELAP_QSChecklist_Final_11222023.pdf), [NY ELAP Certification Manual Item 132, 2024-02-12](https://www.wadsworth.org/sites/default/files/2024-02/Item_132-07.pdf), [TCEQ laboratory accreditation, modified 2026-09-18](https://www.tceq.texas.gov/agency/qa/env_lab_accreditation.html), [25 Pa. Code 252.705](https://www.pacodeandbulletin.gov/Display/pacode?file=/secure/pacode/data/025/chapter252/s252.705.html) |
| EDD formats | [EarthSoft EQuIS lab guide, Oct 2024](https://earthsoft.com/wp-content/uploads/2024/10/EQuIS-EDD-Quick-Reference-Guide-for-Laboratories-2024.pdf), [NYSDEC EDD v5, May 2024](https://dec.ny.gov/sites/default/files/2024-05/eddmanualnewv2024.pdf), [EPA SEDD](https://www.epa.gov/clp/staged-electronic-data-deliverable-sedd), [CA GeoTracker EDF 1.2i](https://geotracker.waterboards.ca.gov/regulators/library/5878544449/LAB_EDF_FormatSpecs_rev11_2023-08-11.pdf), [EPA WQX](https://www.epa.gov/waterdata/water-quality-data-upload-wqx) |
