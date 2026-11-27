import json
import os
import re
import shutil
import socket
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from io import StringIO
from tempfile import TemporaryFile

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.pq import ConnStatus, PGconn

from core.audit import record_event
from core.management.commands import backup_with_inventory
from core.models import LabLogo


def _create_logo(uploader, filename, *, valid=True):
    image = filename.encode("ascii")
    return LabLogo.objects.create(
        filename=filename,
        sha256=sha256(image).hexdigest() if valid else "0" * 64,
        image=image,
        content_type="image/png",
        width=8,
        height=8,
        size=len(image),
        uploaded_by=uploader,
    )


def _live_inventory():
    stdout = StringIO()
    call_command("verify_stored_files", "--format", "manifest", stdout=stdout)
    return json.loads(stdout.getvalue())


def _write_synthetic_dump(snapshot, dump_file):
    dump_file.write(b"PGDMP synthetic archive")


@pytest.fixture
def pg_restore():
    clients = {tool: shutil.which(tool) for tool in ("pg_dump", "pg_restore")}
    versions = []
    for tool, executable in clients.items():
        if executable is None:
            pytest.skip(f"{tool} is required to verify the backup archive")
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10
        )
        version = re.search(r"\(PostgreSQL\) (\d+)", result.stdout)
        if result.returncode or version is None:
            pytest.skip(f"Could not identify the installed {tool} client")
        versions.append(int(version[1]))
    dump_major, restore_major = versions
    if dump_major != connection.pg_version // 10000 or restore_major < dump_major:
        pytest.skip("Compatible pg_dump and pg_restore versions are required to verify the archive")
    return clients["pg_restore"]


@pytest.mark.django_db(transaction=True)
def test_dump_service_uses_selected_multihost_endpoint():
    connection.ensure_connection()
    current = connection.connection.pgconn
    if not current.hostaddr:
        pytest.skip("An IPv4 or IPv6 PostgreSQL connection is required for the multihost test")

    # The first endpoint refuses connections while this socket owns its port without listening.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        params = connection.connection.info.get_parameters()
        params.update(
            host=f"synthetic-unavailable.invalid,{current.host.decode()}",
            hostaddr=f"127.0.0.1,{current.hostaddr.decode()}",
            port=f"{unavailable.getsockname()[1]},{current.port.decode()}",
            password=current.password.decode(),
            connect_timeout=3,
        )
        # Let libpq choose the endpoint so its connection still carries the configured lists.
        raw_multihost = PGconn.connect(make_conninfo(**params).encode())
        with Connection(raw_multihost) as multihost, TemporaryFile() as service_file:
            assert raw_multihost.status == ConnStatus.OK
            endpoint_keys = {b"host", b"hostaddr", b"port"}
            configured = {
                option.keyword: option.val
                for option in raw_multihost.info
                if option.keyword in endpoint_keys
            }
            assert configured == {key: params[key.decode()].encode() for key in endpoint_keys}

            backup_with_inventory._dump_service_file(multihost, service_file)
            service_endpoint = {}
            for line in service_file:
                key, _, value = line.rstrip(b"\n").partition(b"=")
                if key in endpoint_keys:
                    service_endpoint[key] = value

    assert service_endpoint == {
        b"host": current.host,
        b"hostaddr": current.hostaddr,
        b"port": current.port,
    }


@pytest.mark.django_db(transaction=True)
def test_backup_pairs_dump_and_inventory_at_export_time(staff, tmp_path, monkeypatch, pg_restore):
    uploader = staff("synthetic-backup-uploader", "admin")
    original = _create_logo(uploader, "synthetic-before-snapshot.png")
    later_filename = "synthetic-after-snapshot.png"
    before = _live_inventory()
    assert before["categories"]["Laboratory logos"]["count"] == 1
    record_output = StringIO()
    call_command("verify_backup_records", stdout=record_output)
    records_before = json.loads(record_output.getvalue())
    export_snapshot = backup_with_inventory._exported_snapshot

    def commit_later_logo():
        try:
            with transaction.atomic():
                later = _create_logo(uploader, later_filename)
                record_event(actor=uploader, action="synthetic.logo.added", obj=later)
        finally:
            connection.close()

    @contextmanager
    def export_then_commit():
        with export_snapshot() as snapshot:
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                assert cursor.fetchone() == ("repeatable read",)
                cursor.execute("SHOW transaction_read_only")
                assert cursor.fetchone() == ("on",)
            # Commit the new row after export and before either consumer reads.
            with ThreadPoolExecutor(max_workers=1) as writer:
                writer.submit(commit_later_logo).result(timeout=30)
            yield snapshot

    monkeypatch.setattr(backup_with_inventory, "_exported_snapshot", export_then_commit)
    output = tmp_path / "backup"
    call_command("backup_with_inventory", "--output", str(output))

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert {path.name for path in output.iterdir()} == {
        "database.dump",
        "source-files.json",
        "source-records.json",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    inventory = json.loads((output / "source-files.json").read_text(encoding="utf-8"))
    assert inventory["version"] == 1
    assert inventory == before
    records = output / "source-records.json"
    assert json.loads(records.read_text()) == records_before
    with pytest.raises(CommandError, match="audit"):
        call_command("verify_backup_records", "--compare", str(records))

    dump = output / "database.dump"
    with dump.open("rb") as archive:
        assert archive.read(5) == b"PGDMP"
    restore_sql = subprocess.run(
        [pg_restore, "--data-only", "--table=core_lablogo", "--file=-", str(dump)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert original.filename in restore_sql
    assert original.sha256 in restore_sql
    assert later_filename not in restore_sql
    assert connection.get_autocommit()
    assert _live_inventory()["categories"]["Laboratory logos"]["count"] == 2


@pytest.mark.parametrize("existing", ["directory", "file", "dangling symlink"])
def test_backup_rejects_existing_output_without_changing_it(tmp_path, existing):
    output = tmp_path / "backup"
    original = b"original synthetic bytes"
    preserved = output
    if existing == "directory":
        output.mkdir()
        preserved = output / "database.dump"
        preserved.write_bytes(original)
    elif existing == "file":
        output.write_bytes(original)
    else:
        preserved = tmp_path / "missing-target"
        output.symlink_to(preserved)

    with pytest.raises(CommandError, match="already exists"):
        call_command("backup_with_inventory", output=str(output))

    if existing == "dangling symlink":
        assert output.readlink() == preserved
        assert not preserved.exists()
    else:
        assert preserved.read_bytes() == original
        if existing == "directory":
            assert list(output.iterdir()) == [preserved]


@pytest.mark.django_db(transaction=True)
def test_backup_resolves_parent_after_following_symlink(tmp_path, monkeypatch):
    physical_parent = tmp_path / "physical-parent"
    physical_child = physical_parent / "child"
    physical_child.mkdir(parents=True)
    symlink = tmp_path / "link"
    symlink.symlink_to(physical_child, target_is_directory=True)
    lexical_output = tmp_path / "backup"
    lexical_output.mkdir()
    original = lexical_output / "database.dump"
    original.write_bytes(b"existing synthetic archive")
    monkeypatch.setattr(backup_with_inventory, "_dump_database", _write_synthetic_dump)

    call_command("backup_with_inventory", output=str(symlink / ".." / "backup"))

    physical_output = physical_parent / "backup"
    output_files = {path.name for path in physical_output.iterdir()}
    assert output_files == {"database.dump", "source-files.json", "source-records.json"}
    assert list(lexical_output.iterdir()) == [original]
    assert original.read_bytes() == b"existing synthetic archive"


@pytest.mark.django_db(transaction=True)
def test_backup_removes_partial_dump_without_exposing_diagnostics(tmp_path, monkeypatch):
    fake_dump = tmp_path / "pg_dump"
    server_major = connection.pg_version // 10000
    fake_dump.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then\n'
        f"    printf 'pg_dump (PostgreSQL) {server_major}.0\\n'\n"
        "    exit 0\n"
        "fi\n"
        'printf "dump called" > "$0.attempted"\n'
        "printf 'PGDMP partial archive'\n"
        "printf 'synthetic private diagnostic' >&2\n"
        "exit 1\n",
        encoding="ascii",
    )
    fake_dump.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    output = tmp_path / "backup"
    stdout, stderr = StringIO(), StringIO()

    with pytest.raises(CommandError, match="pg_dump failed") as error:
        call_command("backup_with_inventory", output=str(output), stdout=stdout, stderr=stderr)

    assert "synthetic private diagnostic" not in str(error.value)
    assert "synthetic private diagnostic" not in stdout.getvalue() + stderr.getvalue()
    attempted = tmp_path / "pg_dump.attempted"
    assert attempted.read_text(encoding="ascii") == "dump called"
    assert set(tmp_path.iterdir()) == {fake_dump, attempted, output}
    assert list(output.iterdir()) == []
    assert connection.get_autocommit()


@pytest.mark.django_db(transaction=True)
def test_record_summary_failure_discards_both_partial_summaries(tmp_path, monkeypatch):
    def fail_record_summary(name, *args, **kwargs):
        if name == "verify_backup_records":
            kwargs["stdout"].write("partial record summary")
            raise CommandError("Could not read the recovery records")
        return call_command(name, *args, **kwargs)

    monkeypatch.setattr(backup_with_inventory, "call_command", fail_record_summary)
    output = tmp_path / "backup"
    with pytest.raises(CommandError, match="recovery records"):
        call_command("backup_with_inventory", output=str(output))

    assert list(output.iterdir()) == []
    assert connection.get_autocommit()


@pytest.mark.django_db(transaction=True)
def test_backup_leaves_no_output_when_inventory_verification_fails(staff, tmp_path, monkeypatch):
    uploader = staff("synthetic-corrupt-logo-uploader", "admin")
    _create_logo(uploader, "synthetic-incorrect-digest.png", valid=False)
    monkeypatch.setattr(backup_with_inventory, "_dump_database", _write_synthetic_dump)
    output = tmp_path / "backup"

    with pytest.raises(CommandError, match="verification failed"):
        call_command("backup_with_inventory", output=str(output))

    assert list(tmp_path.iterdir()) == [output]
    assert list(output.iterdir()) == []
    assert connection.get_autocommit()
