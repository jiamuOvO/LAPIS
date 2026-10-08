import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from lapis_store import ROOT, db_config, initialize_schema


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated PostgreSQL")
class MigrationTest(unittest.TestCase):
    def test_historical_v2_legacy_checksum_upgrades_once(self):
        secret = json.loads((ROOT / ".postgres-server" / "superuser.json").read_text(encoding="utf-8"))["password"]
        admin = db_config() | {"user": "postgres", "password": secret, "dbname": "postgres"}
        name = "lapis_migration_test_" + uuid4().hex[:12]
        with psycopg.connect(**admin, autocommit=True) as db:
            db.execute(sql.SQL("CREATE DATABASE {} OWNER lapis").format(sql.Identifier(name)))
        connect_test = lambda: psycopg.connect(**(db_config() | {"dbname": name}))
        try:
            first = ROOT / "migrations" / "001_initial.sql"
            second = ROOT / "migrations" / "002_audit_checksums.sql"
            with connect_test() as db:
                for path in (first, second):
                    for statement in path.read_text(encoding="utf-8").split(";"):
                        if statement.strip():
                            db.execute(statement)
                db.execute("UPDATE schema_migrations SET checksum=%s,description='001_initial' WHERE version=1",
                           ("23261c4afa9400c5e6e44448701f877b865530e9c6a8f61fc80c95879bac747c",))
                db.execute("INSERT INTO schema_migrations (version,checksum,description) VALUES (2,%s,'002_audit_checksums')",
                           ("9d933222899ffb4ebc17b381d3a4e4d12dd3a31b940535d1e2e06655c6317a10",))
            legacy = [{"contract_version": 1, "purpose": "旧请求"}, {"contract_version": 2, "original_intent": "旧研究意图", "fields": {}}]
            with connect_test() as db:
                db.execute("INSERT INTO research_tasks (id,intake_state,status) VALUES ('legacy-preservation','{}'::jsonb,'request_confirmed')")
                for version, payload in enumerate(legacy, 1):
                    serialized = json.dumps(payload, ensure_ascii=False)
                    db.execute("INSERT INTO request_versions (task_id,version,payload,payload_sha256,confirmed_by) VALUES ('legacy-preservation',%s,%s::jsonb,%s,'test')",
                               (version, serialized, hashlib.sha256(serialized.encode()).hexdigest()))
            self.assertEqual(initialize_schema(connector=connect_test), [3, 4, 5])
            self.assertEqual(initialize_schema(connector=connect_test), [])
            with connect_test() as db:
                ledger = db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual([row[0] for row in ledger], [1, 2, 3, 4, 5])
            canonical_v1 = hashlib.sha256(first.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            self.assertEqual(ledger[0][1], canonical_v1)
            self.assertEqual(ledger[1][1], "9d933222899ffb4ebc17b381d3a4e4d12dd3a31b940535d1e2e06655c6317a10")
            with connect_test() as db:
                preserved = db.execute("SELECT payload FROM request_versions WHERE task_id='legacy-preservation' ORDER BY version").fetchall()
                self.assertEqual([r[0] for r in preserved], legacy)
                self.assertIsNone(db.execute("SELECT active_request_version FROM research_tasks WHERE id='legacy-preservation'").fetchone()[0])
        finally:
            with psycopg.connect(**admin, autocommit=True) as db:
                db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    def test_v1_upgrade_repeat_and_failed_migration_rollback(self):
        secret = json.loads((ROOT / ".postgres-server" / "superuser.json").read_text(encoding="utf-8"))["password"]
        admin = db_config() | {"user": "postgres", "password": secret, "dbname": "postgres"}
        name = "lapis_migration_test_" + uuid4().hex[:12]
        with psycopg.connect(**admin, autocommit=True) as db:
            db.execute(sql.SQL("CREATE DATABASE {} OWNER lapis").format(sql.Identifier(name)))
        test_config = db_config() | {"dbname": name}
        connect_test = lambda: psycopg.connect(**test_config)
        try:
            first = ROOT / "migrations" / "001_initial.sql"
            with connect_test() as db:
                for statement in first.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        db.execute(statement)
            self.assertEqual(initialize_schema(connector=connect_test), [2, 3, 4, 5])
            self.assertEqual(initialize_schema(connector=connect_test), [])
            with connect_test() as db:
                ledger = db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
            self.assertEqual([row[0] for row in ledger], [1, 2, 3, 4, 5])
            self.assertEqual(ledger[0][1], hashlib.sha256(first.read_bytes().replace(b"\r\n", b"\n")).hexdigest())

            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                for source in (ROOT / "migrations").glob("*.sql"):
                    shutil.copy2(source, directory / source.name)
                (directory / "006_failure_probe.sql").write_text(
                    "CREATE TABLE migration_rollback_probe (id INTEGER); SELECT 1/0;", encoding="utf-8")
                with self.assertRaises(psycopg.errors.DivisionByZero):
                    initialize_schema(directory, connect_test)
                with connect_test() as db:
                    self.assertIsNone(db.execute("SELECT to_regclass('migration_rollback_probe')").fetchone()[0])
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 5)
                (directory / "002_audit_checksums.sql").write_text("SELECT 1;", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "内容与已应用记录不一致"):
                    initialize_schema(directory, connect_test)
        finally:
            with psycopg.connect(**admin, autocommit=True) as db:
                db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    unittest.main()
