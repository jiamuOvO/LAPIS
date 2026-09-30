"""PostgreSQL persistence for LAPIS research objects and audit."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lapis_core import (ExecutionPlan, check_attempt_transition, digest,
                        intake_is_confirmed, validate_research_request_v2, verify_artifact)
from lapis_intake import new_state


ROOT = Path(__file__).resolve().parent
LEGACY_V1_CHECKSUM = "23261c4afa9400c5e6e44448701f877b865530e9c6a8f61fc80c95879bac747c"


def db_config() -> dict:
    local = ROOT / ".lapis-pg.json"
    saved = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    return {
        "host": os.getenv("LAPIS_DB_HOST", saved.get("host", "127.0.0.1")),
        "port": int(os.getenv("LAPIS_DB_PORT", saved.get("port", 5433))),
        "user": os.getenv("LAPIS_DB_USER", saved.get("user", "lapis")),
        "password": os.getenv("LAPIS_DB_PASSWORD", saved.get("password", "")),
        "dbname": os.getenv("LAPIS_DB_NAME", saved.get("dbname", "lapis")),
        "connect_timeout": 8,
    }


def connect():
    return psycopg.connect(**db_config())


def artifact_root() -> Path:
    return ROOT / ("artifacts-test" if db_config()["dbname"] == "lapis_test" else "artifacts")


def initialize_schema(migrations_dir: Path | None = None, connector=None) -> list[int]:
    """Apply numbered PostgreSQL migrations, each in its own locked transaction."""
    directory = migrations_dir or ROOT / "migrations"
    files = sorted(directory.glob("*.sql"))
    versions = []
    for path in files:
        match = re.fullmatch(r"(\d{3})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise ValueError(f"无效迁移文件名：{path.name}")
        versions.append(int(match.group(1)))
    if versions != list(range(1, len(files) + 1)):
        raise ValueError("迁移版本必须从 001 开始连续且唯一")
    if not files:
        raise ValueError("没有数据库迁移文件")

    hashes = {version: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
              for version, path in zip(versions, files)}
    applied_now = []
    for version, path in zip(versions, files):
        with (connector or connect)() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(487064276)")
                exists = cursor.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
                applied = {}
                has_checksum = False
                if exists:
                    has_checksum = bool(cursor.execute(
                        "SELECT 1 FROM information_schema.columns WHERE table_name='schema_migrations' "
                        "AND column_name='checksum'"
                    ).fetchone())
                    columns = "version, checksum" if has_checksum else "version"
                    cursor.execute(f"SELECT {columns} FROM schema_migrations ORDER BY version")
                    applied = {row[0]: row[1] if has_checksum else None for row in cursor.fetchall()}
                    if sorted(applied) != list(range(1, len(applied) + 1)) or max(applied, default=0) > len(files):
                        raise ValueError("数据库迁移历史不连续或存在未知版本")
                if version in applied:
                    legacy_v1 = (version == 1 and applied[version] == LEGACY_V1_CHECKSUM
                                 and max(applied) < 3)
                    if has_checksum and applied[version] != hashes[version] and not legacy_v1:
                        raise ValueError(f"迁移 {version:03d} 的内容与已应用记录不一致")
                    if not has_checksum and version != 1:
                        raise ValueError("迁移记录缺少校验字段")
                    continue
                if max(applied, default=0) != version - 1:
                    raise ValueError(f"不能跳过迁移 {version:03d}")
                for statement in path.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        cursor.execute(statement)
                if version == 1:
                    # The pre-migration v1 schema already inserted its own ledger row.
                    if cursor.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone() is None:
                        raise ValueError("初始迁移未登记版本 1")
                else:
                    cursor.execute("INSERT INTO schema_migrations (version,checksum,description) VALUES (%s,%s,%s)",
                                   (version, hashes[version], path.stem))
                    if version == 2:
                        cursor.execute("UPDATE schema_migrations SET checksum=%s,description=%s WHERE version=1",
                                       (hashes[1], files[0].stem))
            db.commit()
        applied_now.append(version)
    return applied_now


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _event(cursor, task_id: str | None, actor: str, action: str, details: dict | None = None):
    cursor.execute(
        "INSERT INTO audit_events (task_id, actor, action, details) VALUES (%s,%s,%s,%s)",
        (task_id, actor, action, Jsonb(details) if details is not None else None),
    )


def create_task(actor: str) -> str:
    task_id = str(uuid4())
    with connect() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO research_tasks (id, intake_state) VALUES (%s,%s)",
                (task_id, Jsonb(new_state())),
            )
            _event(cursor, task_id, actor, "task_created")
        db.commit()
    return task_id


def get_task(task_id: str) -> dict:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT id, revision, intake_state, intake_result, status FROM research_tasks WHERE id=%s",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("任务不存在")
            row["intake_state"] = _json(row["intake_state"])
            row["intake_result"] = _json(row["intake_result"])
            cursor.execute("SELECT MAX(version) AS version FROM request_versions WHERE task_id=%s", (task_id,))
            row["request_version"] = cursor.fetchone()["version"]
            return row


def get_intake_operation(operation_id: str) -> dict | None:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT task_id, result, request_version, input_sha256 FROM intake_operations WHERE operation_id=%s", (operation_id,))
            return cursor.fetchone()


def save_turn(task_id: str, expected_revision: int, state: dict, result: dict,
              actor: str, model: str, prompt_version: str = "intake-v1",
              operation_id: str | None = None) -> int | None:
    """Optimistic update prevents a slow LLM call from overwriting a newer turn."""
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT revision FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            row = cursor.fetchone()
            if operation_id:
                cursor.execute("SELECT request_version,input_sha256 FROM intake_operations WHERE operation_id=%s AND task_id=%s",
                               (operation_id, task_id))
                previous = cursor.fetchone()
                if previous:
                    input_sha = hashlib.sha256(state["turns"][-1].strip().encode("utf-8")).hexdigest()
                    if previous["input_sha256"] and previous["input_sha256"] != input_sha:
                        raise ValueError("相同操作标识不能用于不同输入")
                    return previous["request_version"]
            if row is None or row["revision"] != expected_revision:
                raise ValueError("会话已被另一轮更新，请重新读取任务")
            confirmed = intake_is_confirmed(state, result)
            status = "request_confirmed" if confirmed else result["intake_status"]
            cursor.execute(
                "UPDATE research_tasks SET revision=revision+1, intake_state=%s, intake_result=%s, status=%s, updated_at=now() WHERE id=%s",
                (Jsonb(state), Jsonb(result), status, task_id),
            )
            version = None
            if confirmed:
                payload = result["request"]
                payload_hash = digest(payload)
                cursor.execute(
                    "SELECT version FROM request_versions WHERE task_id=%s AND payload_sha256=%s",
                    (task_id, payload_hash),
                )
                duplicate = cursor.fetchone()
                if duplicate:
                    version = duplicate["version"]
                else:
                    cursor.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM request_versions WHERE task_id=%s",
                                   (task_id,))
                    version = cursor.fetchone()["version"]
                    cursor.execute(
                        "INSERT INTO request_versions (task_id,version,payload,payload_sha256,confirmed_by) VALUES (%s,%s,%s,%s,%s)",
                        (task_id, version, Jsonb(payload), payload_hash, actor),
                    )
            _event(cursor, task_id, actor, "intake_turn", {
                "model": model, "prompt_version": prompt_version, "revision": expected_revision + 1,
                "request_version": version, "input": state["turns"][-1],
            })
            if operation_id:
                input_sha = hashlib.sha256(state["turns"][-1].strip().encode("utf-8")).hexdigest()
                cursor.execute(
                    "INSERT INTO intake_operations (operation_id,task_id,revision,result,request_version,input_sha256) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (operation_id, task_id, expected_revision + 1, Jsonb(result), version, input_sha),
                )
        db.commit()
    return version


def propose_design(task_id: str, payload: dict, actor: str) -> int:
    """Incomplete designs may be stored for review; approval validates the full contract."""
    if not isinstance(payload, dict):
        raise ValueError("研究设计必须是 JSON 对象")
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            cursor.execute("SELECT MAX(version) AS version FROM request_versions WHERE task_id=%s", (task_id,))
            current = cursor.fetchone()["version"]
            if current is None or payload.get("request_version") != current:
                raise ValueError("设计必须引用当前已确认的研究请求版本")
            cursor.execute("SELECT payload FROM request_versions WHERE task_id=%s AND version=%s",
                           (task_id, current))
            validate_research_request_v2(_json(cursor.fetchone()["payload"]))
            cursor.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM design_versions WHERE task_id=%s", (task_id,))
            version = cursor.fetchone()["version"]
            cursor.execute(
                "INSERT INTO design_versions (task_id,version,request_version,payload,payload_sha256) VALUES (%s,%s,%s,%s,%s)",
                (task_id, version, current, Jsonb(payload), digest(payload)),
            )
            _event(cursor, task_id, actor, "design_proposed", {"design_version": version})
        db.commit()
    return version


def approve_design(task_id: str, version: int, reviewer: str) -> None:
    """Record a reviewer claim; CLI identity is not authenticated for real execution."""
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            cursor.execute("SELECT MAX(version) AS version FROM request_versions WHERE task_id=%s", (task_id,))
            current = cursor.fetchone()["version"]
            cursor.execute(
                "SELECT request_version,payload,status FROM design_versions WHERE task_id=%s AND version=%s FOR UPDATE",
                (task_id, version),
            )
            row = cursor.fetchone()
            if row is None or row["status"] != "proposed" or row["request_version"] != current:
                raise ValueError("设计不存在、已审批，或引用了过期请求")
            ExecutionPlan.model_validate(_json(row["payload"]))
            cursor.execute(
                "UPDATE design_versions SET status='approved', reviewer=%s, reviewed_at=NOW() WHERE task_id=%s AND version=%s",
                (reviewer, task_id, version),
            )
            _event(cursor, task_id, reviewer, "design_approved", {"design_version": version})
        db.commit()


def freeze_execution(task_id: str, version: int, actor: str) -> str:
    """Idempotent freeze. Execution still requires a separately implemented adapter."""
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            cursor.execute("SELECT MAX(version) AS version FROM request_versions WHERE task_id=%s", (task_id,))
            current = cursor.fetchone()["version"]
            cursor.execute(
                "SELECT request_version,payload_sha256,status,reviewer FROM design_versions WHERE task_id=%s AND version=%s",
                (task_id, version),
            )
            design = cursor.fetchone()
            if design is None or design["status"] != "approved" or design["request_version"] != current:
                raise ValueError("仅能冻结引用当前请求且经审核的研究设计")
            cursor.execute("SELECT id FROM execution_requests WHERE task_id=%s AND design_version=%s",
                           (task_id, version))
            existing = cursor.fetchone()
            if existing:
                return existing["id"]
            execution_id = str(uuid4())
            cursor.execute(
                "INSERT INTO execution_requests (id,task_id,design_version,payload_sha256,approved_by) VALUES (%s,%s,%s,%s,%s)",
                (execution_id, task_id, version, design["payload_sha256"], design["reviewer"]),
            )
            _event(cursor, task_id, actor, "execution_frozen", {"execution_id": execution_id})
        db.commit()
    return execution_id


def create_simulation_attempt(execution_id: str, actor: str) -> str:
    """A test substitute may exercise persistence, never scientific evidence."""
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT task_id,status FROM execution_requests WHERE id=%s FOR UPDATE", (execution_id,))
            execution = cursor.fetchone()
            if execution is None or execution["status"] != "frozen":
                raise ValueError("执行请求不存在或不可运行")
            cursor.execute("SELECT attempt,status FROM job_attempts WHERE execution_id=%s ORDER BY attempt DESC LIMIT 1",
                           (execution_id,))
            previous = cursor.fetchone()
            if previous and previous["status"] not in {"failed", "cancelled"}:
                raise ValueError("已有未结束或已成功的尝试；不会重复提交")
            number = previous["attempt"] + 1 if previous else 1
            attempt_id = str(uuid4())
            cursor.execute(
                "INSERT INTO job_attempts (id,execution_id,generation,attempt,status,origin) VALUES (%s,%s,1,%s,'queued','simulation')",
                (attempt_id, execution_id, number),
            )
            _event(cursor, execution["task_id"], actor, "simulation_queued",
                   {"execution_id": execution_id, "attempt_id": attempt_id, "attempt": number})
        db.commit()
    return attempt_id


def transition_attempt(attempt_id: str, target: str, actor: str, error: str | None = None) -> None:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT a.status,e.task_id FROM job_attempts a JOIN execution_requests e ON e.id=a.execution_id "
                "WHERE a.id=%s FOR UPDATE", (attempt_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("作业尝试不存在")
            check_attempt_transition(row["status"], target)
            cursor.execute("UPDATE job_attempts SET status=%s,error_text=%s,updated_at=now() WHERE id=%s",
                           (target, error, attempt_id))
            _event(cursor, row["task_id"], actor, "attempt_status",
                   {"attempt_id": attempt_id, "from": row["status"], "to": target, "error": error})
        db.commit()


def register_artifact(attempt_id: str, kind: str, path: Path, origin: str, actor: str) -> str:
    if origin != "simulation":
        raise ValueError("首版仅有模拟适配器；真实产物须由经审核的计算适配器登记")
    relative, size, sha = verify_artifact(path, artifact_root())
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT a.status,a.origin,a.execution_id,e.task_id FROM job_attempts a "
                "JOIN execution_requests e ON e.id=a.execution_id WHERE a.id=%s FOR UPDATE", (attempt_id,),
            )
            row = cursor.fetchone()
            if row is None or row["status"] != "running" or row["origin"] != origin:
                raise ValueError("仅运行中的匹配来源作业可登记产物")
            if not relative.startswith(f"{row['execution_id']}/{attempt_id}/"):
                raise ValueError("产物必须属于当前作业尝试目录")
            artifact_id = str(uuid4())
            cursor.execute(
                "INSERT INTO raw_artifacts (id,attempt_id,kind,relative_path,size_bytes,sha256,origin) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (artifact_id, attempt_id, kind, relative, size, sha, origin),
            )
            _event(cursor, row["task_id"], actor, "artifact_registered",
                   {"artifact_id": artifact_id, "sha256": sha, "origin": origin})
        db.commit()
    return artifact_id


def verify_artifacts(execution_id: str) -> list[dict]:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT r.id,r.relative_path,r.size_bytes,r.sha256,r.origin FROM raw_artifacts r "
                "JOIN job_attempts a ON a.id=r.attempt_id WHERE a.execution_id=%s", (execution_id,),
            )
            rows = cursor.fetchall()
    for row in rows:
        try:
            relative, size, sha = verify_artifact(artifact_root() / row["relative_path"], artifact_root())
            row["valid"] = (relative, size, sha) == (row["relative_path"], row["size_bytes"], row["sha256"])
        except (OSError, ValueError):
            row["valid"] = False
    return rows
