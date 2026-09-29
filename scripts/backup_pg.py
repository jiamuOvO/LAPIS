"""Create a consistent PostgreSQL backup and SHA-256 manifest."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone


root = Path(__file__).resolve().parents[1]
config = json.loads((root / ".lapis-pg.json").read_text(encoding="utf-8"))
pg_dump = root / ".postgres-server" / "pgsql" / "bin" / "pg_dump.exe"
backup_dir = root / "backups"
backup_dir.mkdir(exist_ok=True)
destination = backup_dir / f"lapis-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.dump"
pending = destination.with_suffix(".dump.tmp")
env = dict(os.environ, PGHOST=config["host"], PGPORT=str(config["port"]),
           PGUSER=config["user"], PGPASSWORD=config["password"])
try:
    subprocess.run([str(pg_dump), "--format=custom", "--no-owner", "--no-privileges",
                    f"--file={pending}", config["dbname"]], env=env, check=True)
    pending.replace(destination)
except Exception:
    pending.unlink(missing_ok=True)
    raise
hasher = hashlib.sha256()
with destination.open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        hasher.update(block)
destination.with_suffix(".sha256").write_text(
    f"{hasher.hexdigest()}  {destination.name}\n", encoding="utf-8")
print(destination)
