#!/usr/bin/env python3
"""Run source-only Infrastructure checks with a disposable Unix-socket PostgreSQL.

Uses the existing .venv and PostgreSQL binaries; reads no .env or credentials.
No TCP listener, external service, worker, migration of an existing DB or backup.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps/api"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, default=Path("/opt/homebrew/opt/postgresql@17/bin"))
    parser.add_argument("--migration-check", action="store_true", help="Check schema drift and rollback/forward 0129")
    parser.add_argument("targets", nargs="*", default=["plane/tests/contract/api/test_infrastructure.py"])
    args = parser.parse_args()
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        parser.error("The component's existing .venv/bin/python is required.")
    pg_bin = args.pg_bin
    if not (pg_bin / "initdb").exists():
        installed = shutil.which("initdb")
        if not installed:
            parser.error("PostgreSQL binaries not found; pass --pg-bin.")
        pg_bin = Path(installed).parent
    with tempfile.TemporaryDirectory(prefix="plane-infra-", dir="/tmp") as directory:
        temporary = Path(directory)
        os.chmod(temporary, 0o700)
        socket = temporary / "socket"
        socket.mkdir(mode=0o700)
        data = temporary / "pg"
        # Deliberate minimal environment: do not inherit runtime credentials or DB URLs.
        environment = {
            "PATH": os.defpath,
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TMPDIR": str(temporary),
            "PYTHONPATH": os.pathsep.join((str(temporary), str(API))),
            "DJANGO_SETTINGS_MODULE": "infrastructure_local_settings",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "PLANE_INFRA_SOCKET": str(socket),
            "PGHOST": str(socket),
            "PGUSER": "plane_infrastructure_test",
            "PGDATABASE": "plane_infrastructure_test",
        }
        (temporary / "infrastructure_local_settings.py").write_text(
            "REDIS_SSL = False\nREDIS_URL = 'redis://127.0.0.1:1/0'\n"
            "from plane.settings.test import *\n"
            "import os\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', "
            "'NAME': 'plane_infrastructure_test', 'USER': 'plane_infrastructure_test', "
            "'HOST': os.environ['PLANE_INFRA_SOCKET'], 'PORT': '5432'}}\n"
            "DATABASE_ROUTERS = []\n"
            "SECRET_KEY = 'synthetic-infrastructure-local-tests-only'\n"
            "CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}\n"
            "CELERY_BROKER_URL = 'memory://'\n"
            "CELERY_RESULT_BACKEND = 'cache+memory://'\n"
            "STORAGES = {'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}, "
            "'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}\n"
            "LOGGING = {'version': 1, 'disable_existing_loggers': True}\n"
        )
        (temporary / "sitecustomize.py").write_text(
            "import socket\n"
            "_connect = socket.socket.connect\n"
            "_connect_ex = socket.socket.connect_ex\n"
            "def guarded(original):\n"
            "    def connect(self, address):\n"
            "        if self.family != socket.AF_UNIX:\n"
            "            raise RuntimeError('Network connections disabled for Infrastructure source checks')\n"
            "        return original(self, address)\n"
            "    return connect\n"
            "socket.socket.connect = guarded(_connect)\n"
            "socket.socket.connect_ex = guarded(_connect_ex)\n"
        )

        def run(command, quiet=False):
            result = subprocess.run(
                [str(part) for part in command],
                cwd=API,
                env=environment,
                text=True,
                stdout=subprocess.PIPE if quiet else None,
                stderr=subprocess.STDOUT,
            )
            if result.returncode:
                if result.stdout:
                    print(result.stdout)
                raise subprocess.CalledProcessError(result.returncode, command)

        started = False
        try:
            run([pg_bin / "initdb", "-D", data, "-U", environment["PGUSER"], "--auth=trust", "--no-locale"], True)
            run(
                [
                    pg_bin / "pg_ctl",
                    "-D",
                    data,
                    "-l",
                    temporary / "postgres.log",
                    "-o",
                    f"-k {socket} -h '' -p 5432",
                    "-w",
                    "start",
                ],
                True,
            )
            started = True
            run([pg_bin / "createdb", "plane_infrastructure_test"])
            print("Disposable PostgreSQL ready on a private Unix socket; TCP disabled.", flush=True)
            if args.migration_check:
                run([python, "manage.py", "makemigrations", "--check", "--dry-run"])
                run([python, "manage.py", "migrate", "--noinput", "--verbosity", "0"])
                run([python, "manage.py", "migrate", "db", "0128", "--noinput", "--verbosity", "0"])
                run([python, "manage.py", "migrate", "db", "0129", "--noinput", "--verbosity", "0"])
                print("Migration forward / 0129 rollback / forward completed.", flush=True)
            run([python, "-m", "pytest", "-o", "addopts=", "--migrations", "-q", *args.targets])
        finally:
            if started:
                subprocess.run(
                    [str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    check=False,
                )


if __name__ == "__main__":
    main()
