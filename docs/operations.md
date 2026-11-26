# Operations: backup, restore and recovery

Runbook for operators of a self-hosted deployment. It covers what to protect and how to prove
a restore. Settings and setup steps live in the [README](../README.md); design rationale in
[architecture](architecture.md). Commands use placeholder credentials; keep real ones in your
shell or secret manager, never in Git, tickets or logs.

## What is protected

All state is in PostgreSQL: results and revisions, custody and audit events, issued report PDFs
and snapshots, subcontract source PDFs, EDD exports and confirmed result import uploads are
kept in the same database. Allow up to 1 MiB of source data per confirmed file import when
sizing the database and its backups, including separate imports of worksheets from one file.
There is no file storage to back up separately. The container filesystem is disposable.

Treat every dump as sensitive. It holds lab records and account data, including each user's
authenticator-app (TOTP) secret and recovery codes: the installed django-allauth stores them
unencrypted unless the deployment configures its encryption hook, which this project does not.
Encrypt backups and restrict who can read them.

`SECRET_KEY` is not stored in the database; keep it in your secret manager. Changing it signs
users out and invalidates outstanding password-reset and email-confirmation links, sign-in codes
and CSRF tokens. It does not make MFA enrolments unusable, because allauth does not derive MFA
secrets from it. Keep the same key when recovering unless you intend those effects.

**Immutability protection, exactly.** Row triggers on the history tables (audit and custody
events, result revisions, issued reports and files, EDD exports, subcontract and external
results, MDL records, uploaded laboratory logos) raise an error on `UPDATE` or `DELETE`. They
do not block `INSERT`, so `pg_restore` works. They do not block `TRUNCATE`, `DROP TABLE` or
schema changes, and the table owner or a superuser can disable them
(`ALTER TABLE ... DISABLE TRIGGER`, `session_replication_role`). Runtime and migration use
the same database role today. Do not repair history in place; restore into a new database
and compare instead. This is not tamper evidence against database administrators.

## Decisions the operator must make

The software does not choose these. Record them before real data.

| Decision | Notes |
| --- | --- |
| Recovery point and time objectives | Render point-in-time recovery covers 3 days on Hobby workspaces and 7 on Pro or higher; Free databases have none ([Render backups](https://render.com/docs/postgresql-backups), checked 2026-11-09). |
| Off-provider destination and encryption | Render keeps logical exports seven days only. Store your own copies encrypted (for example `age` or `gpg`) at a location you control. |
| Retention period and responsible person | Lab policy and regulation decide; the software infers none. |
| Restore-test cadence | Suggested minimum: before go-live and after any major version or migration change. |

## Take a backup

Use PostgreSQL client tools of the same major version as the server (17).
`SOURCE_URL` is the database's external connection string; the Render dashboard shows it, and
your IP must be on the database allow list (the Blueprint default is empty, so none).
If you cannot connect externally, use the dashboard **Recovery → Create export**
instead, which yields a `.dir.tar.gz` directory-format archive for `pg_restore`.

For a backup with a comparable file inventory, use the shared snapshot procedure below.
For a dump alone, run:

```sh
export SOURCE_URL='postgresql://USER@HOST/roots'   # placeholder; put the password in .pgpass
pg_dump --format=directory --jobs=4 --no-owner --file=roots-$(date -u +%Y%m%dT%H%MZ).dump "$SOURCE_URL"
```

Encrypt and copy the result off-provider, then record its date and size. Also capture a
verification summary (see below) beside it so a later restore can be compared.

### Take a backup with a matching file inventory

Use the same password-free `SOURCE_URL` in both terminals, with your usual PostgreSQL
password file. Staff can continue entering records. Run the backup between deployments
so the source schema stays stable.

In the first terminal, run `psql -X --dbname="$SOURCE_URL"` and export a snapshot:

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_export_snapshot();
```

Leave that session open. In a second terminal, in the source application environment, run
the following, pasting the returned ID at the prompt. Set `backup_root` to an existing
private backup directory outside the source checkout.

```sh
backup_root=/path/to/private/backups
printf 'PostgreSQL snapshot ID: '
read -r backup_snapshot
backup_dir=$(mktemp -d "$backup_root/roots-backup-XXXXXXXX") &&
  pg_dump --dbname="$SOURCE_URL" --snapshot="$backup_snapshot" --format=directory --jobs=4 --no-owner \
    --file="$backup_dir/database.dump" &&
  DATABASE_URL="$SOURCE_URL" python manage.py verify_stored_files --snapshot "$backup_snapshot" --format manifest \
    > "$backup_dir/source-files.json"
```

Check the exit status before accepting the pair. If either command fails or the exporting
session disconnects before both tools import the snapshot, start again with a fresh snapshot
and directory. If you also want [source record counts](#verify-the-restore), run the
summary query in the exporting session before closing it. Once both commands finish,
enter `ROLLBACK;` there. Encrypt and retain the successful dump and inventory together. After
restoring `database.dump`, compare the inventories as described below.

PostgreSQL makes an exported snapshot available for import while its transaction is open.
Both readers see the same committed records, including when another session adds a file
between the dump and the inventory check. [PostgreSQL 17 snapshot synchronization](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION-TABLE) · [pg_dump snapshot option](https://www.postgresql.org/docs/17/app-pgdump.html)

## Restore into an isolated database

Never restore over the live database. Create an empty, separate PostgreSQL 17 database (a
second Render database, or the local Compose database on port 55432) and an app environment
that points only at it.

```sh
export TARGET_URL='postgresql://roots:roots@127.0.0.1:55432/roots_restore_check'   # placeholder
psql 'postgresql://roots:roots@127.0.0.1:55432/postgres' -c 'CREATE DATABASE roots_restore_check'
pg_restore --exit-on-error --no-owner --no-privileges --jobs=4 --dbname="$TARGET_URL" roots-YYYYMMDDTHHMMZ.dump
```

- `pg_restore` targets an empty database; `--clean` drops objects first and is not needed here.
- `--single-transaction` gives all-or-nothing behaviour but cannot be combined with `--jobs`.
- Roles named in the dump may not exist on the target; `--no-owner` makes you the owner.
  The runtime role then needs ordinary table privileges on the restored objects.
- Source of semantics: [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).

For a Render point-in-time recovery, the platform creates a **new** database instance; do
not delete the original until you have verified the new one.

## Verify the restore

For a source comparison, run the summary queries in the interactive transaction that exported
the backup snapshot. Alternatively, pause writes before the backup and keep them paused
through all source checks. With a provider export that does not share its snapshot, this
pause is necessary to compare a separately captured inventory or summary. The restored
database can also be checked on its own. Run the restore checks with the source application
version, `DEBUG=false` and `DATABASE_URL` pointing to the restored database.

Save this source summary beside the dump:

```sql
select 'audit', count(*), max(id::text) from core_auditevent
union all select 'custody', count(*), max(id::text) from receipt_custodyevent
union all select 'result_revisions', count(*), max(id)::text from analytical_resultrevision
union all select 'report_versions', count(*), max(id)::text from reporting_reportversion
union all select 'report_files', count(*), null from reporting_reportfile
union all select 'issued_invoices', count(*), max(id)::text from commercial_issuedinvoice;
```

1. `python manage.py migrate --check` exits successfully (no unapplied migrations).
2. The same summary on the restored database equals the saved source summary.
3. Immutability triggers match the source (run on both and diff the output):
   `select tgrelid::regclass, tgname from pg_trigger where not tgisinternal and tgname like '%immutable%' order by 1, 2`
4. Run `python manage.py verify_stored_files` on the restored database. This read-only check
   compares stored bytes with their recorded SHA-256 and, where available, byte count. It
   covers issued report and invoice PDFs (including amendments and voids), subcontract
   source PDFs, electronic deliverable exports, laboratory logos, original result import files and files attached to
   equipment events, training records and nonconformance events. Each issued report version
   and each issued or voided invoice must have its file. For each original and amended
   report, the command also recalculates the checksum of the frozen report content used
   for result downloads. It compares that checksum with the one on the issued version
   and the copy in its snapshot. If the snapshot names a laboratory logo, its SHA-256,
   image type, width and height must match the stored logo identified by that checksum.
   Use the reported version ID to find the original image in the backup, even if the
   laboratory has since replaced or removed its logo. The separate `Laboratory logos`
   check verifies the stored image bytes. Each version must also have exactly one release or
   amendment event with the same issuer, report number, version, predecessor, PDF size,
   PDF checksum and frozen content checksum. Use the report version ID to investigate a
   mismatch. If an event survives without its version, the command reports the audit event
   ID. The `Report PDFs` count includes each version once and each orphan event once.
   For an invoice, the command checks that its draft has exactly one issue event and that
   the recorded invoice number and PDF checksum match. The issuer on the invoice must match
   both the staff member who performed the issue action and the issuer recorded in its details.
   A voided invoice must still match its original issue event. Its void event must show
   the transition from issued to voided. The invoice number and PDF checksum must match
   before and after the void. The staff member, reason and time of the void must agree
   with the restored draft at the precision recorded in the audit event. An invoice still
   marked issued must have no void event.
   Under `Issued invoice PDFs`, use the invoice ID to investigate a status or history
   discrepancy. If the document is missing from an issued or voided draft, the check
   reports the draft once. An issue or void event whose target is missing or invalid
   is reported by audit event ID.
   For an electronic data deliverable, check the `EDD export ZIPs` category. Each ZIP
   must have exactly one original export event with the same ZIP checksum, report number,
   version number and report content checksum. Use the export record ID to investigate
   a mismatch, or the audit event ID to trace a missing export record.
   Under `Subcontract source PDFs`, each original and corrected source needs one matching
   result entry for its dispatch and source version. The staff member, source record ID,
   PDF byte count and SHA-256 must agree with that entry. Use the subcontract result ID
   to trace a file with inconsistent history, or the audit event ID to find an entry whose
   source is missing.
   Under `Equipment event evidence`, the command compares attached calibration and maintenance
   files with the audit entry for the original work or correction. It checks the filename,
   checksum, equipment, event, kind of work, staff member who recorded it and any event it
   corrected. A surviving entry that mentions an attachment can reveal a missing file or
   equipment event. Use the reported equipment event or audit event ID to investigate.
   Under `Training record evidence`, each attached file must match the filename and checksum
   in the original training or correction event. The event must identify the same record,
   trainee, staff member who entered it and, for a correction, the record it corrected.
   If the file or the entire training record is missing, a surviving event that recorded an
   attachment still triggers a failure. Use the reported record or event ID to investigate.
   Training recorded without an attachment passes this check.
   Under `Nonconformance event evidence`, a note attachment needs one matching audit entry
   for the same note, nonconformance, staff member and file checksum. A surviving entry
   can identify a missing note or a note whose attachment fields are all empty. Use the
   reported timeline or audit event ID to investigate. For a file attached when staff open,
   advance, return or reopen a nonconformance, new audit entries record the timeline ID and
   file checksum alongside the status. The check uses that entry to identify a missing or
   replaced attachment, including a file lost with its timeline entry. Earlier opening and
   status entries lack these file references. Compare the restored inventory with the one
   saved at backup to detect a missing file when no audit reference survives.
   An optional evidence attachment with only part of its file or metadata present also fails.
   To save just the counts as JSON, run `python manage.py verify_stored_files --format json > stored-file-counts.json`.
   Each category and the total give the number checked and the number of records with failures.
   Keep the counts with the restore record and check the exit status. A nonzero exit signals a failure.
   Use the reported record IDs to investigate on the restored copy. PostgreSQL reads every
   stored file for this check, so allow time proportional to the volume of files.
   To compare the full set of files, save a source inventory when you take the backup.
   The [shared snapshot procedure](#take-a-backup-with-a-matching-file-inventory) produces
   `source-files.json` while the application is accepting writes. If writes are paused
   throughout the backup and source checks, run this on the source instead:

   ```sh
   python manage.py verify_stored_files --format manifest > source-files.json
   ```

   On the restored copy, before accepting writes or applying migrations, use the same
   application version. Connect it to the restored database and supply the inventory saved
   with that backup:

   ```sh
   python manage.py verify_stored_files --compare-manifest source-files.json
   ```

   A match prints the number of files checked and exits zero. A missing or malformed
   inventory, different category set, changed count or different digest exits nonzero.
   For a count difference, the message gives both counts. For a digest difference at the
   same count, investigate missing or replaced records in the named category. If the
   saved inventory includes categories the application does not recognize, check that
   the two application versions match. If stored
   file verification itself fails, use the affected record IDs printed on standard error.
   On an older application version that only supports inventory generation, generate
   `restored-files.json` with `--format manifest` and compare it to `source-files.json`
   with `diff -u` after verifying that generation succeeded.
   The inventory contains a count and a SHA-256 digest per category, including empty
   categories. The digest covers the file records' identities, byte lengths and content
   checksums. A difference identifies the affected category even if the file count is
   unchanged. The inventory omits filenames and document contents. Use the other restore
   checks for custody, audit history and metadata outside this inventory.
5. Start the app against the restored database, check `/ready/`, sign in as an administrator
   and download one issued report.
6. On a scratch copy only, an `UPDATE core_auditevent SET reason = reason` must fail with
   "Historical records cannot be updated or deleted".

Record the result (date, backup file, checks passed) in your own change log. A backup that
has not passed these checks is untested.

## Recover production

Pick the situation first; they are different procedures.

**Bad release, database healthy.** Redeploy the previous code if it is compatible with the
current schema. Project policy is backward-compatible migrations, so the previous release usually runs
against a newer schema, but check the migrations added since it (a dropped or renamed column
breaks it). Do not restore the
database: that would discard records entered since the backup.

Migration `analytical.0015` adds storage for new confirmed file imports. When rolling back
application code, keep the migrated schema and its files. Migration reversal is refused
once a source file exists. Older application versions can read the existing results but
cannot download source files or retain new uploads.

One case to check: holding-time rules that name an analyte (migration `analytical.0009`). A
release from before that change reads every active rule as method-wide, so it holds each
analyte of the method to the shortest of those limits (stricter, never a silent pass), and its
rule form, import and seed can refuse or pick the wrong row. Create analyte rules only once no
instance of an older release is running, and deactivate them before running an older release
against the database. The migration's reverse refuses while any rule names an analyte.

Client account-hold notices add defaulted columns in `receipt.0013`; existing clients start
without a hold. Keep the additive schema on a code rollback. Older code does not show these
notices, so staff must check account instructions separately until compatible code returns.
No normal deployment or migration changes an existing client's hold or resets demo data.

Draft invoices add two tables in `commercial.0005`. Reversing refuses once a draft exists;
keep them during a code rollback. Older code can continue running but cannot show or edit
these drafts. Return to compatible code to
use the retained billing snapshots and audit history. Include the tables in normal database
backups; a deploy neither creates invoices nor resets the demonstration.

Quote-based pricing adds nullable provenance fields in `commercial.0006` and permits a quote
instead of a price list. Keep these fields on code rollback; reversal refuses once quote
provenance exists. Earlier code can still create list-priced drafts but cannot correctly present
or edit quote-priced drafts. Use compatible code for those records. Existing invoices and issued
quotes are not rewritten, and deployment does not seed or reset data.

Quote fee and discount lines add a defaulted kind in `commercial.0007` and allow their
sample/test references to be empty. Existing lines remain tests, and a database default supports
previous code inserting test lines during deployment. Reversal refuses once a fee or discount
line exists, including cancelled drafts. Keep the schema on code rollback and use compatible
code to view or edit drafts with these lines: earlier code cannot correctly present or edit them.
This migration does not change accepted quotes or existing draft amounts, and does not reset data.

Manual fees and discounts relax only the quote-source requirement for non-test lines in
`commercial.0008`; no rows or amounts are rewritten. Keep that constraint expansion on code
rollback. Previous code can still read and cancel these drafts, but cannot edit drafts containing manual
lines, add manual lines or label their manual origin. Use compatible code for these records. Reversal
refuses once a manual line exists, including on cancelled drafts; never invent a quote source
to force reversal. Normal backup/restore covers these lines and their audit history.

Draft due dates add a nullable column in `commercial.0009`. Existing drafts stay undated.
Keep the column on code rollback: previous code can create and edit drafts but neither shows
nor changes their dates. Reversal refuses while any draft, including a cancelled one, has a
date. Do not clear dates to force reversal; use compatible code to inspect or change them with
a reason. Normal backup/restore includes the dates and their audit history. Deployment does
not infer dates, seed invoices or reset data.

Manual tax lines expand `commercial.0010`'s invoice line constraint; existing drafts and amounts
are unchanged. Keep the schema on code rollback. Previous code can read, add a fee or discount
to, and cancel a draft with tax lines while preserving its total, but cannot label Tax or edit
those lines; use compatible code for presentation and editing. Reversal refuses while any tax
line exists, including zero amounts and cancelled drafts. Never delete or relabel retained
assessments to force reversal. Normal backup/restore includes tax lines and audit history.
Deployment does not seed invoices or reset data.

Invoice issuance in `commercial.0011` adds immutable snapshots, numbered PDF bytes and
their digests. The database allows one active draft or issued invoice per order. Existing
drafts and cancellations retain their identifiers and amounts. Reversing the migration
refuses once an issued document exists, including a subsequently voided invoice. Keep the
schema on code rollback. Wait until every web instance runs the new code before issuing
the first invoice. Older instances offer to create a draft on an already-issued order, which
the database rejects. Use compatible code for billing on those orders. Include issued PDFs,
void history and the immutability trigger in restore checks.

BOD bottle worksheets add the nullable `bottle_data` column in `analytical.0012`. Keep that column during a code rollback: reversing the migration refuses once any bottle snapshot exists. Older code cannot show or reproduce those bottle observations and cannot accept entries for a BOD calculation rule; deactivate that rule before using older code for new entries, and return to a compatible release to inspect retained worksheets. A normal deploy does not reseed; the synthetic BOD example appears at the demonstration’s next scheduled reset.

Run calibration evidence adds `analytical.0013`: an immutable calibration table and nullable
result-provenance fields. Keep these objects during a code rollback; reversing refuses once a
calibration record exists. Older code can run with this additive schema but cannot display the
new evidence or enforce its selected-check and contributor gates. Do not review or release
calibration-linked work on older code. Restore a compatible release to inspect and use it.
Normal deployment does not reseed the demonstration.

**Data loss or corruption.** Recover into a new database; never overwrite or discard the
current one.

1. Stop writes (suspend the web service) and keep the damaged database untouched.
2. Restore a backup or point-in-time recovery into a new database and verify it as above.
3. Records entered after the backup exist only in the damaged database. Export or
   reconcile them with the lab before choosing which database becomes production.
4. Point `DATABASE_URL` at the chosen database and deploy; pre-deploy runs
   `migrate --noinput`, a no-op if the schema is current.
5. Keep the other database until the lab signs off.

A migration that must be reverted needs a deliberately written reverse or a staged fix, not a
blind restore. Take a backup before each deploy that adds migrations.

Receipt migration `0014_order_report_package` adds an order preference with a database default
of false. Earlier report versions keep their original PDF and results. Keep the column during
a code rollback so order preferences survive. Older workers can still insert orders, but omit
QC summary requests when issuing or amending reports, even for an order already marked for a
summary. Use upgraded workers to issue or amend any report that needs the summary.

## Check the rehearsed upgrade

Open the GitHub Actions `checks` run for the revision you plan to deploy. Its application
job summary gives the starting and candidate commits. Compare the starting commit with
your installation. For a pull request, the candidate is the merge commit GitHub tested.
Sign in to GitHub. Under **Artifacts** on the run summary, download `upgrade-rehearsal-<attempt>` for the
successful application job attempt. Extract `upgrade-notes.md` to keep with your upgrade
records. The rehearsal restores a PostgreSQL backup into an empty database, then checks the restored
records, four PDF downloads and an owning client's electronic deliverable ZIP download
with the previous application. A staff reviewer also downloads an equipment event attachment,
a training record attachment and both files from a nonconformance (the opening and a later
note). The files must match the originals, and a signed-in client must be denied each download. CI repeats those checks on
the same copy after migration. The table counts the baseline rows verified at each stage.
A separate table lists the files the candidate checked on the migrated copy with
`verify_stored_files`. A zero identifies a file category absent from that rehearsal.
The published report confirms that PostgreSQL rejected updates and deletions on a baseline
audit event, custody event, issued report version and stored report PDF at both stages.
It also confirms that a result worksheet and one of the readings used in its calculation
each rejected an edit and a deletion.
The rehearsal runs those checks only on its disposable restored copy.
The synthetic source database stays at its original schema throughout.
Orders, samples and containers have separate counts. The receipt check follows the original
client sample ID, its correction and the custody transfer to the sample’s current location.

Use the migration list and changes to locked package versions to plan the upgrade. Read the
new or revised changelog technical notes for setup and rollback instructions. Each note links
to its entry at the tested candidate commit. If the starting revision predates changelog
entries, the summary includes all candidate technical notes for reference. Check the migration
list to see which migrations this rehearsal applied. Follow the recovery link for rollback
considerations at that revision. The rehearsal
checks a populated synthetic installation. Run acceptance on your own installation after
applying the migrations.

## Acceptance run after installation and upgrade

`run_acceptance` runs scripted checks of the first journey and the controls it rests on (role
refusals, client isolation, independent review, release permission, holding time, QC gates,
a result calculated from its readings, a soil result taken back to the sample on a dry-weight
basis, a two-endpoint titration with retained burette readings and titrant strength, a BOD mean with retained bottle selections and entered seed-correction sources, report
immutability and amendments, audit events) on
synthetic records, and writes an evidence
package for the laboratory to keep. Run it after installing, after each upgrade once the
release's migrations have run, and after changing the database server or container image.

```sh
python manage.py run_acceptance --operator "Full Name" --output /path/to/new-directory
```

It never uses the laboratory's records. It creates a database named
`acceptance_<database name>` on the same server, migrates it from empty, runs the steps and
drops it; this takes well under a minute. The database role needs `CREATEDB`. Where it has not,
create an empty database for acceptance runs, owned by the application's role (the role must
own the database's current schema so the run can empty it), and pass its URL in
`ACCEPTANCE_DATABASE_URL` (or `--scratch-database-url`, which shows the password in the process
list). The run refuses a URL that reaches the working database, however it is written, and a
database that holds anything other than an earlier acceptance run, and empties it afterwards.
On the working database the run only reads the migration table and the server's identity and,
in the default mode, holds an advisory lock and creates and drops the scratch database; a
second run started meanwhile is refused.

Every connection that creates, marks, empties or drops checks first that it reached the
database it should and not the working one. If such a check fails, the run stops and cleans up
nothing; the message names what exists. Ctrl-C ends the run and removes the scratch database.
A killed process leaves `acceptance_<database name>` behind (marked by an
`acceptance_scratch_marker` table); the next run reports it and stops until you pass
`--drop-stale-scratch-database`. `--keep-scratch-database` leaves it for inspection; it then
holds the synthetic accounts' password hashes, so drop it when done.

The run uses the deployment's own settings. Run it with `DEBUG=false` and the production
settings; with `DEBUG=true` the report says the production protections were off. Requests carry
what the configured proxy settings expect: the `X-Forwarded-Proto` header of
`SECURE_PROXY_SSL_HEADER`, and the client-address header named by
`ALLAUTH_TRUSTED_CLIENT_IP_HEADER` or an `X-Forwarded-For` with `ALLAUTH_TRUSTED_PROXY_COUNT`
entries, each with `127.0.0.1`. The run signs in fewer than fifteen times, within the sign-in
rate limits. Inside the container, write to a mounted directory the image's user (uid 10001)
can write, or the package is lost when the container exits; on a hosted service, copy the
package off the instance before the shell closes.

The package, in a new or empty directory (default: `environmental-lims-acceptance-<UTC time>` in the
current directory, never inside the static or website directories). The run creates the
directory and tests that it can write there before anything else:

| File | Contents |
| --- | --- |
| `report.html`, `report.pdf` | Software version and commit (where the environment or a Git checkout supplies it), Python, Django and PostgreSQL versions, time zone and security settings, migrations, the operator as stated, start and finish times, every step with requirement, expected and observed result, and blank Performed by and Reviewed by lines |
| `evidence.json` | The same, with every observation and the full migration list |
| `artifacts/` | The issued and amended report PDFs the run produced |
| `manifest-sha256.txt` | SHA-256 of every other file; check with `sha256sum -c manifest-sha256.txt` |

| Exit status | Meaning |
| --- | --- |
| 0 | Every step passed and the package is complete |
| 1 | The package was written, but a step failed or did not run, `report.pdf` could not be rendered, or the scratch database could not be removed (the report says which) |
| 2 | Refused or stopped before any step ran; the message says whether anything was created |
| 3 | Unexpected failure. No package was written, or files without a manifest were left because the package could not be completed; the message says which |

Keep the package with the laboratory's other records of the installation; sign the printed
report as your procedure requires.

What it does not show: anything about the laboratory's own methods, limits, catalog, accounts
or records (it uses the synthetic `DEMO-` method set), backups and restore, mail delivery,
browser behaviour or performance. Requests are made in process, so TLS, the proxy and Gunicorn
are not exercised; check `/ready/` over the real address separately. A passing run is a record
that these checks passed on this installation at that time, not a validation of the software
or the laboratory; the laboratory decides what it means in its quality system.

## Sign-in limits behind a proxy

Failed sign-ins, password resets and sign-in code requests are limited per account and per
client address. The address is the connecting peer unless `ALLAUTH_TRUSTED_PROXY_COUNT` says how
many trusted proxies append to `X-Forwarded-For`. Behind a hosting platform's proxy the peer is
the proxy for every visitor, so with the default of `0` all visitors share one per-address limit:
repeated failed sign-ins by anyone can block sign-in for everyone until the window passes.

Set `ALLAUTH_TRUSTED_PROXY_COUNT` to the number of proxies you control or trust between the
visitor and the application. Verify it before relying on it: sign in from a known address and
confirm that address is the entry that many places from the end of the `X-Forwarded-For` header
your service receives. A count that is too high lets a visitor forge the address and escape the
limit; if the header has fewer entries than the count, the request fails. The Blueprint does not
set a value because the platform's proxy chain must be confirmed on the running service.

Some platforms instead put the visitor's address in a header of their own and replace whatever a
client sends in it. If yours documents such a header, set `ALLAUTH_TRUSTED_CLIENT_IP_HEADER` to
its name; it is then used instead of the proxy count. Use it only for a header the platform
guarantees: a header that a client can set lets anyone choose the address they are counted under.

Limit counters are kept in each worker process's memory. With several Gunicorn workers or
instances the effective limit is multiplied accordingly; configure a shared Django cache if you
need exact limits.

## Browser security headers

Every response carries a Content Security Policy that allows scripts, styles, images, fonts and
connections from the deployment's own origin only, with no inline scripts or styles, and a
Permissions-Policy that turns off camera, microphone, geolocation, payment and USB access. The
application's own pages need nothing more. If a deployment adds approved sources of its own, set
`CONTENT_SECURITY_POLICY` to the complete replacement policy; it is sent as given, so keep
`frame-ancestors 'none'` and avoid `'unsafe-inline'` and `'unsafe-eval'`.

## Request size

The application refuses a request that declares a body larger than `MAX_REQUEST_BODY_BYTES`
(12 MiB by default; the largest upload a form accepts is 10 MiB) before reading it. This checks
the declared length only. Keep a body limit on the proxy or load balancer in front of the
application as well, because a request without a declared length is not covered here.

## HTTPS policy

With `DEBUG=false` the application redirects HTTP to HTTPS, marks its cookies secure and sends
`Strict-Transport-Security` for one hour. The short default limits the damage of a mistake.
Once every host that serves the deployment works over HTTPS only, raise
`SECURE_HSTS_SECONDS` (one year is `31536000`). Set `SECURE_HSTS_INCLUDE_SUBDOMAINS=true` only if
every subdomain of the host is served over HTTPS as well; browsers remember the policy for the
whole period and it cannot be withdrawn early.

## Logs

Each request writes one line to standard output (retained by the platform) from
`core/request_logging.py`: request ID, method, matched URL route pattern, status and duration.
The raw path is never logged because URL segments carry reset and confirmation tokens and record
identifiers; a request shows `route=accounts/confirm-email/<key>/`, or `<unmatched>` for paths
that match nothing. The ID is generated by the server, never taken from a client header, and is
returned in the `X-Request-ID` response header. Quote it to find related errors.

Sign-in activity writes `roots.signin` lines to the same stdout stream: `signin.success`,
`signin.failed` and `signout`, with the stored username. Unknown or ambiguous identifiers show
`user=<unknown>`; submitted login text, passwords and codes are never logged. Usernames are
URL-escaped so they cannot add log lines or fields. Password failures include attempts refused
by the sign-in rate limit; rejected email sign-in codes for known accounts and rejected MFA
sign-in codes also log a failure. Success is logged once, after all required authentication
stages complete. Explicit account and administration sign-outs are logged; anonymous sign-outs,
abandoned authentication stages and browser/session expiry do not emit a sign-out event.
These are operational logs, separate from laboratory audit history and database backups; a demo
database reset does not erase the platform's existing logs. Treat usernames as personal data and
set an appropriate retention period or stream to a controlled destination for longer history.

On Render, open the web service's **Logs** page and search for `roots.signin` (or an event such
as `signin.failed`). Every application log line also has `render_request_id`, the validated
`Rndr-Id` header, or `-` when absent or malformed. This field is for correlation only; it never
replaces the server-generated `request_id` or grants trust to the caller. Search that Render ID
to find the associated HTTP request log when the workspace has Pro or higher. Runtime log
retention follows the workspace's plan. Verify the header reaches the app
with a sign-in after deployment. Source: [Render logging](https://render.com/docs/logging),
checked 2026-11-12.

Django's own error and security records are rewritten to the same ID, status, route and exception
class. Tracebacks keep their frames but exception messages are withheld, because Django and
libraries can embed request details in them. Query strings, bodies, cookies, other headers and
local variables are not logged; usernames appear only in the sign-in activity lines described
above. Frame lines show source code text, never values.

Limit: Gunicorn's access log and the development server's own request line (both log the raw path)
are off, and Gunicorn's error log stays on for worker-level problems. Messages that application code logs itself are not rewritten, so
do not log submitted values. Credentials, `DATABASE_URL`, `SECRET_KEY` and SMTP passwords belong only
in environment settings or the provider's secret store.

## Public demonstration

A public demonstration lets anyone sign in with shared accounts, so it must return to a known
state by itself. This is for a deployment that holds only the seed's synthetic records. Do not
do any of it where a laboratory keeps its records.

`seed_demo --announce-accounts` publishes the shared accounts on the sign-in page and stops them
changing their own credentials (see the README). `reset_demo` then erases every record and runs
the seed again:

```sh
ALLOW_DEMO_RESET=true python manage.py reset_demo --full --announce-accounts
```

It asks for confirmation unless `--noinput` is given, and refuses to run unless all of these hold:

| Check | Why |
| --- | --- |
| `ALLOW_DEMO_RESET=true` in the job's environment | The deployment opts in; a laboratory's deployment never sets it |
| The database's laboratory is the seed's "Example Environmental Lab", or the database is empty | A database set up with `setup_lab` is never erased |
| No migration is pending and none is unknown to the release | A job from another release than the web service would seed the wrong schema |

The erase and the seed are one transaction. Requests that arrive meanwhile wait, usually well
under a minute, then see the fresh demonstration; everyone is signed out. If the seed fails, the
transaction rolls back and nothing changes. The erase empties every application table, including
accounts, sessions, custody and audit history and issued reports, and restarts identifiers such
as sample accession numbers. The database triggers that keep history rows from being updated or
deleted do not apply to this whole-table erase; that is deliberate and is the reason for the
checks above.

On Render the schedule is `render.demo.yaml`, a Blueprint separate from `render.yaml`. The deploy
button and a laboratory's Blueprint never create it. In the workspace that already runs the
demonstration from `render.yaml`:

1. Deploy the release to the web service first; its pre-deploy step migrates.
2. Create a new Blueprint from the same repository and branch with **Blueprint Path**
   `render.demo.yaml`. It adds one paid cron job, `smart-roots-lims-demo-reset`, and refers to the
   existing web service and database by name. Render asks once for `DEMO_PASSWORD`, which will be
   public, and `DEMO_ADMIN_PASSWORD`, which must differ and stay private.
3. Use **Trigger Run** on the job to reset straight away, or wait for the schedule (daily at
   08:00 UTC). Change the time in the file's `schedule` and `DEMO_RESET_NOTE` together.

Both files set auto-deploy off, the right default for a laboratory: deploy the job and the web
service together for every release, or the job refuses to run until they match.

The public demonstration instead shows the current `develop` branch. Its web service and reset job
both track `develop` with Render's **After CI Checks Pass** auto-deploy (`checksPass`), set on the
two services (2026-11-12) rather than in the files. Render waits for the `checks` workflow, which
cancels runs superseded by a newer push, so several pushes in a row end in one deploy of the last
commit and a commit that fails its checks is not deployed. Keep branch and trigger equal on both
services. Syncing the demonstration's Blueprint from `render.yaml` would turn the web service's
auto-deploy off again; its automatic sync is off. A deploy migrates but does not reseed: changed
seed data appears at the next reset.

The job is billed for the time it runs, with a monthly minimum;
check [Render pricing](https://render.com/pricing).

Sources, checked 2026-11-12: a Blueprint file may sit at a custom path and one repository may back
several independent Blueprints ([Render Blueprints](https://render.com/docs/infrastructure-as-code));
cron schedules use UTC, at most one run is active at a time and billing is by running time with a
monthly minimum ([cron jobs](https://render.com/docs/cronjobs)); a Blueprint may reference a
service that exists in the workspace but is not in the file
([Blueprint reference](https://render.com/docs/blueprint-spec)); `checksPass` deploys a commit
only after its CI checks pass ([deploys](https://render.com/docs/deploys)). `render blueprints validate
render.demo.yaml` (CLI 2.36.0) accepted the file against the demonstration's workspace on that
date, planning one new service; it creates nothing.
