"""Restore the latest backup into a temporary database and compare core row counts."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql


root = Path(__file__).resolve().parents[1]
backup = max((root / "backups").glob("*.dump"), key=lambda path: path.stat().st_mtime)
expected = backup.with_suffix(".sha256").read_text(encoding="utf-8").split()[0]
hasher = hashlib.sha256()
with backup.open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        hasher.update(block)
if hasher.hexdigest() != expected:
    raise SystemExit("备份文件 SHA-256 不匹配")
config = json.loads((root / ".lapis-pg.json").read_text(encoding="utf-8"))
secret = json.loads((root / ".postgres-server" / "superuser.json").read_text(encoding="utf-8"))["password"]
restore_name = "lapis_restore_" + uuid4().hex[:12]
admin = dict(host=config["host"], port=config["port"], user="postgres",
             password=secret, dbname="postgres")
tables = ("research_tasks", "request_versions", "design_versions", "execution_requests",
          "job_attempts", "raw_artifacts", "audit_events", "intake_operations")
checkpoint_tables = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")
with psycopg.connect(**admin, autocommit=True) as db:
    db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore_name)))
try:
    pg_restore = root / ".postgres-server" / "pgsql" / "bin" / "pg_restore.exe"
    env = dict(os.environ, PGHOST=config["host"], PGPORT=str(config["port"]),
               PGUSER="postgres", PGPASSWORD=secret)
    subprocess.run([str(pg_restore), "--no-owner", "--no-privileges",
                    f"--dbname={restore_name}", str(backup)], env=env, check=True)
    with psycopg.connect(**config) as original, psycopg.connect(**(admin | {"dbname": restore_name})) as restored:
        for table in tables + checkpoint_tables:
            original_exists = original.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None
            restored_exists = restored.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None
            if original_exists != restored_exists:
                raise RuntimeError(f"{table}: 原库与恢复库的表存在状态不一致")
            if not original_exists:
                continue
            original_count = original.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            restored_count = restored.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            if original_count != restored_count:
                raise RuntimeError(f"{table}: {original_count} != {restored_count}")
    print("恢复演练通过：业务与已有图检查点表的记录数一致，备份哈希有效")
finally:
    with psycopg.connect(**admin, autocommit=True) as db:
        db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(restore_name)))
