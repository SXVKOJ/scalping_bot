#!/usr/bin/env python3
"""Wait for Postgres, apply migrations, seed defaults, then exec the service command."""
import os
import socket
import subprocess
import sys
import time


def wait_for_postgres(timeout: int = 90) -> None:
    host = os.environ.get("POSTGRES_HOST", "scalpingdb")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"PostgreSQL is reachable at {host}:{port}", flush=True)
                return
        except OSError:
            print(f"Waiting for PostgreSQL at {host}:{port}...", flush=True)
            time.sleep(1)
    print(f"ERROR: PostgreSQL at {host}:{port} is not reachable", file=sys.stderr, flush=True)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: entrypoint.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    wait_for_postgres()
    subprocess.check_call([sys.executable, "manage.py", "migrate", "--noinput"])
    subprocess.check_call([sys.executable, "manage.py", "ensure_defaults"])
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
