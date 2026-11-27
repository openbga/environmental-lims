import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import Error as DjangoDatabaseError
from django.db import connection, transaction
from psycopg import Connection as PsycopgConnection
from psycopg import Error as PsycopgError
from psycopg.pq import TransactionStatus

_DUMP = "database.dump"
_INVENTORY = "source-files.json"
_DUMP_PARTIAL = ".database.dump.partial"
_INVENTORY_PARTIAL = ".source-files.json.partial"


@contextmanager
def _exported_snapshot():
    if connection.vendor != "postgresql":
        raise CommandError("A paired backup requires PostgreSQL.")
    try:
        if connection.in_atomic_block or not connection.get_autocommit():
            raise CommandError("A paired backup requires a fresh PostgreSQL transaction.")
    except DjangoDatabaseError, ImproperlyConfigured, PsycopgError:
        raise CommandError("Could not connect to PostgreSQL for the paired backup.") from None

    raw_connection = connection.connection
    if not isinstance(raw_connection, PsycopgConnection):
        raise CommandError("A paired backup requires the Psycopg 3 PostgreSQL driver.")
    if raw_connection.info.transaction_status != TransactionStatus.IDLE:
        raise CommandError("A paired backup requires a fresh PostgreSQL transaction.")

    try:
        with transaction.atomic(durable=True):
            with raw_connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SELECT pg_catalog.pg_export_snapshot()")
                (snapshot,) = cursor.fetchone()
            yield snapshot
    except DjangoDatabaseError, PsycopgError:
        raise CommandError("A PostgreSQL error interrupted the paired backup.") from None


def _matching_pg_dump(server_version):
    executable = shutil.which("pg_dump")
    if executable is None:
        raise CommandError("Install pg_dump on the backup host and put it on PATH.")
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError, subprocess.CalledProcessError:
        raise CommandError("Could not run pg_dump on the backup host.") from None
    version = re.match(rb"pg_dump \(PostgreSQL\) ([0-9]+)\.", result.stdout)
    if version is None or int(version[1]) != server_version // 10000:
        raise CommandError("Use a pg_dump with the same major version as the PostgreSQL server.")
    return executable


def _dump_service_file(raw_connection, service_file):
    env = os.environb.copy()
    env.pop(b"DATABASE_URL", None)
    env[b"PGSERVICEFILE"] = os.fsencode(f"/dev/fd/{service_file.fileno()}")
    service_file.write(b"[openbga_backup]\n")
    pgconn = raw_connection.pgconn
    options = pgconn.info
    # Pin the selected endpoint, retaining the hostname for TLS certificate validation.
    resolved = {b"host": pgconn.host, b"hostaddr": pgconn.hostaddr, b"port": pgconn.port}
    if pgconn.password:
        # Use the password that libpq selected for this host, including from a passfile.
        resolved[b"password"] = pgconn.password
    # pg_dump supplies its own fallback name, which takes precedence over a service file.
    # Use the configured fallback as the application name when no explicit name was given.
    names = {option.keyword: option.val for option in options}
    if not names.get(b"application_name") and names.get(b"fallback_application_name"):
        resolved[b"application_name"] = names[b"fallback_application_name"]
    for option in options:
        if option.envvar:
            env.pop(option.envvar, None)
        if option.keyword in {b"service", b"fallback_application_name"}:
            continue
        value = resolved.get(option.keyword, option.val)
        if value is None:
            continue
        line = option.keyword + b"=" + value
        # libpq service files have a line length limit and strip trailing whitespace.
        if len(line) >= 1022 or b"\n" in value or value != value.rstrip():
            if option.keyword == b"password":
                env[b"PGPASSWORD"] = value
                continue
            raise CommandError(
                "A PostgreSQL connection setting cannot be passed safely to pg_dump."
            )
        service_file.write(line + b"\n")
    service_file.flush()
    service_file.seek(0)
    return env


def _dump_database(snapshot, dump_file):
    raw_connection = connection.connection
    executable = _matching_pg_dump(raw_connection.info.server_version)
    args = [
        executable,
        "--format=custom",
        "--no-password",
        "--dbname=service=openbga_backup",
        f"--snapshot={snapshot}",
    ]
    with raw_connection.cursor() as cursor:
        cursor.execute("SELECT current_user, session_user")
        current_user, session_user = cursor.fetchone()
        if current_user != session_user:
            args.append(f"--role={current_user}")

    with tempfile.TemporaryFile() as service_file:
        env = _dump_service_file(raw_connection, service_file)
        try:
            subprocess.run(
                args,
                check=True,
                env=env,
                pass_fds=(service_file.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=dump_file,
                stderr=subprocess.DEVNULL,
            )
        except OSError, subprocess.CalledProcessError:
            raise CommandError(
                "pg_dump failed. Check the PostgreSQL client, connection and backup permissions."
            ) from None
    dump_file.seek(0)
    if dump_file.read(5) != b"PGDMP":
        raise CommandError("pg_dump did not produce a PostgreSQL custom-format archive.")


@contextmanager
def _output_directory(path):
    if not path.name:
        raise CommandError("Backup output already exists. Choose a new path.")
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
            os.fsync(parent)
            fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
    except FileExistsError:
        raise CommandError("Backup output already exists. Choose a new path.") from None
    except OSError:
        raise CommandError("Could not create a private backup output directory.") from None

    try:
        yield fd
    except OSError:
        raise CommandError("Could not write the paired backup. Use a new path to retry.") from None
    finally:
        os.close(fd)


def _create_file(name, mode, directory):
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, dir_fd=directory)
    return os.fdopen(fd, mode, encoding=None if "b" in mode else "utf-8")


class Command(BaseCommand):
    help = "Save a PostgreSQL dump and a version 1 stored-file inventory from the same snapshot."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, metavar="PATH", help="New backup directory.")

    def handle(self, *args, **options):
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            raise CommandError("Run the paired backup on a Linux or macOS backup host.")
        with _output_directory(Path(options["output"])) as directory:
            created = []
            try:
                with _exported_snapshot() as snapshot:
                    with _create_file(_INVENTORY_PARTIAL, "w", directory) as inventory:
                        created.append(_INVENTORY_PARTIAL)
                        # The verifier uses the already established transaction for this scan.
                        call_command(
                            "verify_stored_files",
                            "--format",
                            "manifest",
                            stdout=inventory,
                            stderr=self.stderr,
                        )
                        inventory.flush()
                        os.fsync(inventory.fileno())
                    with _create_file(_DUMP_PARTIAL, "w+b", directory) as dump:
                        created.append(_DUMP_PARTIAL)
                        _dump_database(snapshot, dump)
                        os.fsync(dump.fileno())

                # Hard links keep an existing destination intact. The manifest appears last.
                for partial, final in ((_DUMP_PARTIAL, _DUMP), (_INVENTORY_PARTIAL, _INVENTORY)):
                    os.link(partial, final, src_dir_fd=directory, dst_dir_fd=directory)
                    created.append(final)
                    os.unlink(partial, dir_fd=directory)
                    created.remove(partial)
                os.fsync(directory)
            except BaseException:
                for filename in reversed(created):
                    with suppress(OSError):
                        os.unlink(filename, dir_fd=directory)
                raise
