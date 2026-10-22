"""PostgreSQL persistence for LAPIS research objects and audit."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from pydantic import ValidationError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lapis_core import (ExecutionPlan, check_attempt_transition, digest,
                        ensure_finite_numbers, intake_is_confirmed, validate_research_request_v2,
                        verify_artifact)
from lapis_intake import new_state
from lapis_design import DesignDraft, changed_paths, design_input_sha256, normalize_design_gaps
from lapis_contract import CONTRACT_VERSION, FIELDS, validate_current_request


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
                "SELECT id, revision, intake_state, intake_result, status, active_request_version FROM research_tasks WHERE id=%s",
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


def get_current_request(task_id: str) -> tuple[int, dict]:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT status,active_request_version,intake_state FROM research_tasks WHERE id=%s", (task_id,))
            task = cursor.fetchone()
            if not task or task["status"] != "request_confirmed" or task["active_request_version"] is None:
                raise ValueError("当前任务没有已确认的有效研究请求")
            version = task["active_request_version"]
            cursor.execute("SELECT payload FROM request_versions WHERE task_id=%s AND version=%s",
                           (task_id, version))
            row = cursor.fetchone()
            payload = _json(row["payload"])
            validate_current_request(payload)
            state = _json(task["intake_state"])
            if state.get("stage") != "ready_for_design" or state.get("draft_id") != payload.get("draft_id"):
                raise ValueError("当前受理草稿已变化，请重新确认后再生成研究设计")
            return version, payload


def get_design_operation(task_id: str, operation_id: str) -> dict | None:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT version,payload FROM design_versions WHERE task_id=%s "
                "AND payload->'generation'->>'operation_id'=%s",
                (task_id, operation_id),
            )
            row = cursor.fetchone()
            if row:
                row["payload"] = _json(row["payload"])
            return row


def get_design_suggestion_operation(task_id: str, operation_id: str) -> dict | None:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute("SELECT details FROM audit_events WHERE task_id=%s "
                                 "AND action='design_revision_suggested' "
                                 "AND details->>'operation_id'=%s ORDER BY id DESC LIMIT 1",
                                 (task_id, operation_id)).fetchone()
            return _json(row["details"]) if row else None


def save_design_suggestion(task_id: str, envelope: dict, actor: str) -> tuple[dict, bool]:
    operation_id = envelope.get("operation_id")
    input_sha256 = envelope.get("input_sha256")
    if not operation_id or not input_sha256:
        raise ValueError("设计修订建议缺少 operation_id 或输入指纹")
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            task = cursor.execute("SELECT status,active_request_version FROM research_tasks WHERE id=%s FOR UPDATE",
                                  (task_id,)).fetchone()
            if not task or task["status"] != "request_confirmed" or task["active_request_version"] != envelope.get("request_version"):
                raise ValueError("生成建议期间研究请求已改变，请重新生成")
            parent = cursor.execute("SELECT payload_sha256,request_version FROM design_versions "
                                    "WHERE task_id=%s AND version=%s", (task_id, envelope.get("parent_version"))).fetchone()
            if not parent or parent["request_version"] != envelope["request_version"] or \
                    parent["payload_sha256"].strip() != envelope.get("parent_sha256"):
                raise ValueError("生成建议期间父设计已改变")
            previous = cursor.execute("SELECT details FROM audit_events WHERE task_id=%s "
                                      "AND action='design_revision_suggested' AND details->>'operation_id'=%s "
                                      "ORDER BY id DESC LIMIT 1", (task_id, operation_id)).fetchone()
            if previous:
                saved = _json(previous["details"])
                if saved.get("input_sha256") != input_sha256:
                    raise ValueError("相同操作标识已用于不同修订建议输入")
                return saved["envelope"], True
            _event(cursor, task_id, actor, "design_revision_suggested", {
                "operation_id": operation_id, "input_sha256": input_sha256,
                "parent_version": envelope["parent_version"], "parent_sha256": envelope["parent_sha256"],
                "request_version": envelope["request_version"], "model": envelope.get("model"),
                "prompt_sha256": envelope.get("prompt_sha256"), "envelope": envelope,
                "adopted": False,
            })
        db.commit()
    return envelope, False


def get_design(task_id: str, version: int) -> dict:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT payload,status,request_version,payload_sha256 "
                           "FROM design_versions WHERE task_id=%s AND version=%s",
                           (task_id, version))
            row = cursor.fetchone()
            if row is None:
                raise ValueError("研究设计版本不存在")
            return {"task_id": task_id, "version": version, **row, "payload": _json(row["payload"])}


def get_latest_design_version(task_id: str) -> int | None:
    with connect() as db:
        with db.cursor() as cursor:
            return cursor.execute("SELECT MAX(version) FROM design_versions WHERE task_id=%s",
                                  (task_id,)).fetchone()[0]


def get_request_version(task_id: str, version: int) -> dict:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute("SELECT payload,payload_sha256 FROM request_versions WHERE task_id=%s AND version=%s",
                                 (task_id, version)).fetchone()
            if row is None:
                raise ValueError("研究请求版本不存在")
            return {"payload": _json(row["payload"]), "payload_sha256": row["payload_sha256"].strip()}


def _pointer_value(value: dict, pointer: str):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("缺口依据须使用 JSON Pointer")
    current = value
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return None
    return current


def _has_gap_evidence(value) -> bool:
    if isinstance(value, str):
        text = value.strip().casefold()
        return bool(text) and not any(token in text for token in ("待定", "待确认", "tbd", "todo", "unknown"))
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return bool(value) and all(_has_gap_evidence(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and all(_has_gap_evidence(item) for item in value)
    return False


def _gap_category(gap: dict) -> str | None:
    text = f"{gap.get('field', '')} {gap.get('question', '')}".casefold()
    categories = {
        "model": ("model", "力场", "模型"),
        "method": ("method", "方法"),
        "quality": ("quality", "收敛", "质量判据", "质量检查"),
        "unit": ("unit", "单位", "量纲"),
        "condition": ("condition", "条件", "温度", "压力"),
        "parameter": ("parameter", "参数", "采样", "步数"),
        "raw_files": ("raw file", "原始文件", "轨迹", "输出文件"),
        "composition": ("composition", "配方", "组成", "浓度"),
        "budget": ("budget", "资源", "时限", "运行规模"),
    }
    for category, terms in categories.items():
        if any(term in text for term in terms):
            return category
    return None


def _validate_gap_pointer(payload: dict, gap: dict, disposition: dict) -> None:
    category = _gap_category(gap)
    if category is None:
        raise ValueError(f"缺口 {gap.get('id')} 类型无法绑定到执行字段；须由研究人员说明不适用或明确缺口类型")
    allowed_fields = {
        "model": {"model", "model_version"}, "method": {"method", "method_version"},
        "quality": {"quality_rules"}, "unit": {"observable_unit", "observable_dimension", "observable_unit_review"},
        "condition": {"conditions"}, "parameter": {"parameters"},
        "raw_files": {"expected_raw_files"}, "composition": {"composition"},
        "budget": {"estimated_resources", "total_runs", "max_walltime_minutes", "scope"},
    }
    pointer = disposition.get("plan_pointer", "")
    match = re.fullmatch(r"/(experiments|candidates)/(\d+)/([^/]+)|/budget/([^/]+)", pointer)
    if not match:
        raise ValueError(f"缺口 {gap.get('id')} 必须指向对应的执行输入字段，不能指向处置或来源元数据")
    collection, index_text, field, budget_field = match.groups()
    field = budget_field or field
    if field not in allowed_fields[category]:
        raise ValueError(f"缺口 {gap.get('id')} 的依据路径与缺口类型不匹配")
    rows = payload.get(collection, []) if collection else [payload.get("budget", {})]
    index = int(index_text) if index_text is not None else 0
    if index >= len(rows) or not _has_gap_evidence(rows[index].get(field)):
        raise ValueError(f"阻断缺口 {gap.get('id')} 的依据字段不存在或为空")
    # A decision about a shared model, method, quality rule, sampling setup, or
    # condition applies to every execution row generated from the source design.
    scoped_fields = {
        "model": ("model", "model_version"), "method": ("method", "method_version"),
        "quality": ("quality_rules",), "unit": ("observable_unit",),
        "condition": ("conditions",), "parameter": ("parameters",),
        "raw_files": ("expected_raw_files",),
    }.get(category, ())
    for experiment in payload.get("experiments", []):
        if any(not _has_gap_evidence(experiment.get(name)) for name in scoped_fields):
            raise ValueError(f"缺口 {gap.get('id')} 尚未覆盖所有适用执行实验")


def _check_bound_execution_plan(cursor, task_id: str, version: int, payload: dict, request_version: int):
    if payload.get("schema") != "lapis.execution-plan.v2":
        raise ValueError("执行计划缺少 v2 来源契约；须从已保存设计导出并补全")
    provenance = payload.get("provenance") or {}
    if payload.get("request_version") != request_version or provenance.get("request_version") != request_version:
        raise ValueError("执行计划绑定的研究请求版本不一致")
    current_request = _require_current_request(cursor, task_id, request_version)
    request_sha = digest(current_request)
    if provenance.get("request_sha256") != request_sha:
        raise ValueError("执行计划研究请求来源哈希不一致")
    source_version = provenance.get("source_design_version")
    source_sha = provenance.get("source_design_sha256")
    if not isinstance(source_version, int) or source_version >= version:
        raise ValueError("执行计划必须引用更早的已保存设计版本")
    source_row = cursor.execute(
        "SELECT payload,payload_sha256,request_version FROM design_versions WHERE task_id=%s AND version=%s",
        (task_id, source_version)).fetchone()
    if source_row is None or source_row["request_version"] != request_version:
        raise ValueError("执行计划引用的设计版本不存在或请求已过期")
    source = normalize_design_gaps(_json(source_row["payload"]))
    if source_sha != source_row["payload_sha256"].strip() or digest(source) != source_sha:
        raise ValueError("执行计划来源设计哈希不一致")
    dispositions = {item.get("gap_id"): item for item in payload.get("gap_dispositions", [])}
    for gap in source.get("open_questions", []):
        if not gap.get("blocks_execution", True):
            continue
        disposition = dispositions.get(gap.get("id")) or {}
        status = disposition.get("status")
        if status == "resolved":
            _validate_gap_pointer(payload, gap, disposition)
            if len((disposition.get("reason") or "").strip()) < 12 or not disposition.get("basis_fields"):
                raise ValueError(f"已解决缺口 {gap.get('id')} 须填写处置理由和请求依据字段")
        elif status == "not_applicable":
            if len((disposition.get("reason") or "").strip()) < 12 or not disposition.get("basis_fields"):
                raise ValueError(f"不适用缺口 {gap.get('id')} 须填写具体理由和请求依据字段")
        else:
            raise ValueError(f"仍有未解决的执行阻断缺口：{gap.get('id')} — {gap.get('question')}")
        if set(disposition.get("basis_fields", [])) - set(FIELDS):
            raise ValueError(f"缺口 {gap.get('id')} 引用了不存在的研究请求依据字段")
    plan = ExecutionPlan.model_validate(payload)
    plan_data = plan.model_dump()
    if plan.paradigm != source.get("paradigm"):
        raise ValueError("执行计划范式必须与来源设计一致")
    source_candidates = {item["id"] for item in source.get("candidates", [])}
    plan_candidates = {item["id"] for item in plan_data["candidates"]}
    if plan_candidates != source_candidates:
        raise ValueError("执行计划候选 ID 必须与来源设计候选完全对应")
    source_experiments = {item["id"] for item in source.get("experiments", [])}
    if any(item.get("source_experiment_id") not in source_experiments for item in plan_data["experiments"]):
        raise ValueError("每项执行实验都必须引用来源设计中的实验 ID")
    source_by_id = {item["id"]: item for item in source.get("experiments", [])}
    for item in plan_data["experiments"]:
        eligible = set(source_by_id[item["source_experiment_id"]].get("candidate_ids", []))
        if item["candidate_id"] not in eligible:
            raise ValueError("执行实验候选不属于其引用的来源实验")
    hypothesis_ids = {item.get("id") for item in source.get("hypotheses", []) if item.get("id")}
    for experiment in plan_data["experiments"]:
        if not set(experiment.get("hypothesis_ids", [])) <= hypothesis_ids:
            raise ValueError("执行实验引用了不存在的机制假设")
    if plan.paradigm == "comparison":
        if not plan.comparison_pairs or any(len(pair) != 2 or pair[0] == pair[1] or not set(pair) <= plan_candidates
                                            for pair in plan.comparison_pairs):
            raise ValueError("比较范式须明确至少一组有效候选比较关系")
        for left, right in plan.comparison_pairs:
            shared_sources = {source_id for source_id, row in source_by_id.items()
                              if {left, right} <= set(row.get("candidate_ids", []))}
            covered = any({left, right} <= {experiment["candidate_id"] for experiment in plan_data["experiments"]
                           if experiment["source_experiment_id"] == source_id}
                          for source_id in shared_sources)
            if not covered:
                raise ValueError(f"比较关系 {left}/{right} 缺少同一来源实验下双方的执行覆盖")
    if plan.paradigm == "screening" and not plan.screening_criteria:
        raise ValueError("筛选范式须明确评价/筛选依据")
    if plan.paradigm == "mechanism_verification":
        if not hypothesis_ids or set(plan.hypothesis_tests) != hypothesis_ids:
            raise ValueError("机制验证须为来源假设指定检验实验")
        for hypothesis_id in hypothesis_ids:
            if not plan.hypothesis_tests.get(hypothesis_id):
                raise ValueError(f"机制假设 {hypothesis_id} 缺少检验实验")
        experiment_ids = {item["id"] for item in plan_data["experiments"]}
        if any(not set(ids) <= experiment_ids for ids in plan.hypothesis_tests.values()):
            raise ValueError("机制假设检验引用了不存在的执行实验")
        for hypothesis_id, mapped_ids in plan.hypothesis_tests.items():
            tagged_ids = {item["id"] for item in plan_data["experiments"]
                          if hypothesis_id in item.get("hypothesis_ids", [])}
            if set(mapped_ids) != tagged_ids:
                raise ValueError(f"机制假设 {hypothesis_id} 与执行实验的双向映射不一致")
        if any(not item.get("predicted_observations") or not item.get("falsifier")
               for item in source.get("hypotheses", [])):
            raise ValueError("机制假设须含可观测预测和反驳条件")
    budget = plan.budget
    if budget is None or not budget.scope.strip() or not budget.estimated_resources:
        raise ValueError("执行计划须明确资源估算、总运行规模、最长总时限和批准范围")
    if any(not key.strip() or not value.strip() for key, value in budget.estimated_resources.items()):
        raise ValueError("资源估算名称与取值不得为空")
    if budget.total_runs < len(plan_data["experiments"]):
        raise ValueError("总运行规模不能少于执行实验数量")
    if budget.max_walltime_minutes < sum(item["max_walltime_minutes"] for item in plan_data["experiments"]):
        raise ValueError("总最长时限不能小于各执行实验时限之和")
    if plan.execution_scope not in {"research", "software_test"}:
        raise ValueError("执行计划须明确区分研究范围或软件测试范围")
    return plan


def check_design(task_id: str, version: int) -> dict:
    record = get_design(task_id, version)
    task = get_task(task_id)
    with connect() as db:
        with db.cursor() as cursor:
            latest_design = cursor.execute("SELECT MAX(version) FROM design_versions WHERE task_id=%s",
                                           (task_id,)).fetchone()[0]
    report = {"task_id": task_id, "design_version": version,
              "request_version": record["request_version"], "design_sha256": record["payload_sha256"].strip(),
              "current_request": task["active_request_version"] == record["request_version"] and
                                task["status"] == "request_confirmed",
              "status": record["status"], "approval_ready": False,
              "research_suitability": "requires_researcher_review", "issues": []}
    if version != latest_design:
        report["issues"].append(f"不是最新设计版本；当前最新为 v{latest_design}")
    payload = record["payload"]
    if digest(payload) != report["design_sha256"]:
        report["issues"].append("保存内容与数据库哈希不一致")
    if not report["current_request"]:
        report["issues"].append("引用的研究请求不是当前已确认版本")
    if payload.get("schema") == "lapis.execution-plan.v2":
        try:
            with connect() as db:
                with db.cursor(row_factory=dict_row) as cursor:
                    _check_bound_execution_plan(cursor, task_id, version, payload, record["request_version"])
            report["approval_ready"] = not report["issues"] and record["status"] == "proposed"
        except (ValueError, ValidationError) as error:
            report["issues"].append(str(error))
    else:
        for gap in normalize_design_gaps(dict(payload)).get("open_questions", []):
            if gap.get("blocks_execution", True):
                report["issues"].append(f"未定项 {gap['id']} 阻断执行：{gap['question']}")
        report["issues"].append("设计草案尚未转换为来源绑定的完整执行计划")
    if record["status"] != "proposed":
        report["issues"].append(f"当前设计状态为 {record['status']}，需新建版本后重新审核")
    return report


def get_intake_operation(operation_id: str) -> dict | None:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT task_id, result, request_version, input_sha256, input_context FROM intake_operations WHERE operation_id=%s", (operation_id,))
            return cursor.fetchone()


def save_turn(task_id: str, expected_revision: int, state: dict, result: dict,
              actor: str, model: str, prompt_version: str = "intake-v1",
              operation_id: str | None = None, input_context: dict | None = None) -> int | None:
    """Optimistic update prevents a slow LLM call from overwriting a newer turn."""
    input_context = input_context or {}
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT revision,active_request_version FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            row = cursor.fetchone()
            if operation_id:
                cursor.execute("SELECT request_version,input_sha256,input_context FROM intake_operations WHERE operation_id=%s AND task_id=%s",
                               (operation_id, task_id))
                previous = cursor.fetchone()
                if previous:
                    input_sha = hashlib.sha256(state["turns"][-1].strip().encode("utf-8")).hexdigest()
                    if previous["input_sha256"] and previous["input_sha256"] != input_sha:
                        raise ValueError("相同操作标识不能用于不同输入")
                    if _json(previous["input_context"]) != input_context:
                        raise ValueError("相同操作标识不能用于不同确认或选择上下文")
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
            active = row["active_request_version"]
            if confirmed and result.get("request", {}).get("contract_version") == CONTRACT_VERSION:
                active = version
            elif result.get("content_changed") or state.get("contract_version") != CONTRACT_VERSION or state.get("stage") != "ready_for_design":
                active = None
            cursor.execute("UPDATE research_tasks SET active_request_version=%s WHERE id=%s", (active, task_id))
            _event(cursor, task_id, actor, "intake_turn", {
                "model": model, "prompt_version": prompt_version, "revision": expected_revision + 1,
                "request_version": version, "input": state["turns"][-1],
                "input_context": input_context, "metadata": result.get("metadata", {}),
            })
            if operation_id:
                input_sha = hashlib.sha256(state["turns"][-1].strip().encode("utf-8")).hexdigest()
                cursor.execute(
                    "INSERT INTO intake_operations (operation_id,task_id,revision,result,request_version,input_sha256,input_context) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (operation_id, task_id, expected_revision + 1, Jsonb(result), version, input_sha, Jsonb(input_context)),
                )
        db.commit()
    return version


def _require_current_request(cursor, task_id: str, version: int) -> dict:
    row = cursor.execute("SELECT status,active_request_version,intake_state FROM research_tasks WHERE id=%s FOR UPDATE",
                         (task_id,)).fetchone()
    if not row or row["status"] != "request_confirmed" or row["active_request_version"] != version:
        raise ValueError("当前请求没有有效的当前规则确认版本，请先重新核对并确认")
    record = cursor.execute("SELECT payload FROM request_versions WHERE task_id=%s AND version=%s",
                            (task_id, version)).fetchone()
    payload = _json(record["payload"])
    validate_current_request(payload)
    state = _json(row["intake_state"])
    if state.get("stage") != "ready_for_design" or state.get("draft_id") != payload["draft_id"]:
        raise ValueError("当前草稿已经变化，请重新确认")
    return payload


def _store_design(task_id: str, payload: dict, actor: str, *, generated: bool) -> tuple[int, dict, bool]:
    if not isinstance(payload, dict):
        raise ValueError("研究设计必须是 JSON 对象")
    normalize_design_gaps(payload)
    ensure_finite_numbers(payload)
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            current = payload.get("request_version")
            request = _require_current_request(cursor, task_id, current)
            operation_id = (payload.get("generation") or {}).get("operation_id")
            if generated:
                generation = payload.get("generation") or {}
                if payload.get("schema") != "lapis.design-draft.v1" or not operation_id:
                    raise ValueError("模型草案缺少受支持的结构或操作标识")
                if (not generation.get("model") or not re.fullmatch(r"[0-9a-f]{64}",
                                                                      generation.get("provider_sha256", ""))):
                    raise ValueError("模型草案缺少有效的模型或提供方指纹")
                expected_input = design_input_sha256(
                    request, generation.get("model", ""), generation.get("provider_sha256", ""))
                if generation.get("request_sha256") != digest(request) or generation.get("input_sha256") != expected_input:
                    raise ValueError("相同操作标识不能用于不同研究请求、模型或提示词")
            if operation_id:
                cursor.execute(
                    "SELECT version,request_version,payload FROM design_versions WHERE task_id=%s "
                    "AND payload->'generation'->>'operation_id'=%s",
                    (task_id, operation_id),
                )
                previous = cursor.fetchone()
                if previous:
                    if previous["request_version"] != current:
                        raise ValueError("相同操作标识已用于旧版研究请求")
                    saved = _json(previous["payload"])
                    if generated and saved.get("generation", {}).get("input_sha256") != expected_input:
                        raise ValueError("相同操作标识已用于不同输入")
                    return previous["version"], saved, True
            cursor.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM design_versions WHERE task_id=%s", (task_id,))
            version = cursor.fetchone()["version"]
            cursor.execute(
                "INSERT INTO design_versions (task_id,version,request_version,payload,payload_sha256) VALUES (%s,%s,%s,%s,%s)",
                (task_id, version, current, Jsonb(payload), digest(payload)),
            )
            _event(cursor, task_id, actor, "design_proposed", {"design_version": version})
        db.commit()
    return version, payload, False


def propose_design(task_id: str, payload: dict, actor: str) -> int:
    """Store a manual or incomplete draft; approval validates the full execution contract."""
    return _store_design(task_id, payload, actor, generated=False)[0]


def propose_generated_design(task_id: str, payload: dict, actor: str) -> tuple[int, dict, bool]:
    """Atomically deduplicate a generated draft and return the persisted canonical result."""
    return _store_design(task_id, payload, actor, generated=True)


def design_edit_document(task_id: str, version: int, actor: str) -> dict:
    record = get_design(task_id, version)
    task = get_task(task_id)
    if task["active_request_version"] != record["request_version"]:
        raise ValueError("不能从过期请求的设计导出修订")
    if get_latest_design_version(task_id) != version:
        raise ValueError("父设计已过期；请基于最新版本重新导出")
    return {"schema": "lapis.design-revision.v1", "task_id": task_id,
            "parent_version": version, "parent_sha256": record["payload_sha256"].strip(),
            "request_version": record["request_version"], "operation_id": "填写唯一操作标识",
            "actor": actor, "reason": "填写修改理由", "payload": record["payload"]}


def _validate_revision_envelope(cursor, task_id: str, envelope: dict, *, require_latest: bool = True) -> tuple[dict, dict, str]:
    if envelope.get("schema") != "lapis.design-revision.v1" or envelope.get("task_id") != task_id:
        raise ValueError("修订文件类型或任务 ID 不匹配")
    parent_version = envelope.get("parent_version")
    operation_id = envelope.get("operation_id")
    reason = (envelope.get("reason") or "").strip()
    if not isinstance(parent_version, int) or not operation_id or operation_id == "填写唯一操作标识" or len(reason) < 4:
        raise ValueError("修订须指定父版本、唯一操作标识和修改理由")
    current = cursor.execute("SELECT status,active_request_version FROM research_tasks WHERE id=%s FOR UPDATE",
                             (task_id,)).fetchone()
    if not current or current["status"] != "request_confirmed" or current["active_request_version"] is None:
        raise ValueError("当前任务没有有效的已确认研究请求")
    if envelope.get("request_version") != current["active_request_version"]:
        raise ValueError("修订文件引用了过期请求版本")
    parent = cursor.execute("SELECT payload,payload_sha256,request_version,status FROM design_versions "
                            "WHERE task_id=%s AND version=%s FOR UPDATE", (task_id, parent_version)).fetchone()
    if parent is None or parent["request_version"] != envelope["request_version"]:
        raise ValueError("父设计不存在或请求版本已过期")
    if envelope.get("parent_sha256") != parent["payload_sha256"].strip():
        raise ValueError("父版本哈希变化；请重新导出并人工合并")
    latest = cursor.execute("SELECT MAX(version) AS version FROM design_versions WHERE task_id=%s",
                            (task_id,)).fetchone()["version"]
    if require_latest and latest != parent_version:
        raise ValueError(f"父版本已过期；当前最新设计为 v{latest}，不自动合并")
    return _json(parent["payload"]), envelope.get("payload"), operation_id


def preview_design_revision(task_id: str, envelope: dict) -> dict:
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            parent, proposed, operation_id = _validate_revision_envelope(cursor, task_id, envelope)
    if not isinstance(proposed, dict) or proposed.get("schema") != parent.get("schema"):
        raise ValueError("修订内容必须保持父设计的契约类型")
    if proposed.get("request_version") != parent.get("request_version") or proposed.get("generation") != parent.get("generation"):
        raise ValueError("修订不能改写请求版本或最初生成来源")
    if parent.get("schema") == "lapis.execution-plan.v2":
        old_provenance = dict(parent.get("provenance") or {})
        new_provenance = dict(proposed.get("provenance") or {})
        old_provenance.pop("operation_id", None)
        new_provenance.pop("operation_id", None)
        if old_provenance != new_provenance:
            raise ValueError("执行计划修订不能改写请求/来源设计版本或哈希")
    if proposed.get("revision") != parent.get("revision"):
        raise ValueError("修订文件不能伪造历史修订链")
    if proposed.get("schema") == "lapis.design-draft.v1":
        normalize_design_gaps(proposed)
        DesignDraft.model_validate({key: proposed.get(key) for key in
                                    ("paradigm", "rationale", "candidates", "experiments", "hypotheses",
                                     "open_questions", "limitations")})
    elif proposed.get("schema") == "lapis.execution-plan.v2":
        ExecutionPlan.model_validate(proposed)
    diff = changed_paths(parent, proposed)
    if not diff:
        raise ValueError("修订没有内容变化")
    return {"parent_version": envelope["parent_version"], "operation_id": operation_id,
            "changed_paths": diff, "proposal": proposed, "adopted": False}


def propose_design_revision(task_id: str, envelope: dict, actor: str | None = None) -> tuple[int, dict, bool]:
    operation_id = envelope.get("operation_id")
    if operation_id:
        with connect() as db:
            with db.cursor(row_factory=dict_row) as cursor:
                parent, proposed, _ = _validate_revision_envelope(cursor, task_id, envelope, require_latest=False)
                input_hash = digest({"parent_sha256": envelope.get("parent_sha256"), "payload": proposed,
                                     "reason": envelope.get("reason"),
                                     "request_version": envelope.get("request_version")})
                previous = cursor.execute("SELECT version,payload FROM design_versions WHERE task_id=%s "
                                          "AND payload->'revision'->>'operation_id'=%s",
                                          (task_id, operation_id)).fetchone()
                if previous:
                    saved = _json(previous["payload"])
                    if saved.get("revision", {}).get("input_sha256") != input_hash:
                        raise ValueError("相同操作标识已用于不同修订输入")
                    return previous["version"], saved, True
    preview = preview_design_revision(task_id, envelope)
    parent_version = envelope["parent_version"]
    operation_id = envelope["operation_id"]
    payload = preview["proposal"]
    revision_input = digest({"parent_sha256": envelope["parent_sha256"], "payload": payload,
                             "reason": envelope["reason"], "request_version": envelope["request_version"]})
    payload = dict(payload)
    payload.pop("revision", None)
    if payload.get("schema") == "lapis.execution-plan.v2":
        payload["provenance"] = dict(payload["provenance"])
        payload["provenance"]["operation_id"] = operation_id
    payload["revision"] = {"parent_version": parent_version, "parent_sha256": envelope["parent_sha256"],
                           "operation_id": operation_id, "actor": actor or envelope.get("actor", "researcher"),
                           "reason": envelope["reason"].strip(), "changed_paths": preview["changed_paths"],
                           "input_sha256": revision_input, "origin": "researcher_adopted"}
    ensure_finite_numbers(payload)
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            current_parent, _, _ = _validate_revision_envelope(cursor, task_id, envelope, require_latest=False)
            if digest(current_parent) != envelope["parent_sha256"]:
                raise ValueError("父版本哈希变化；请重新导出并人工合并")
            previous = cursor.execute("SELECT version,payload FROM design_versions WHERE task_id=%s "
                                      "AND payload->'revision'->>'operation_id'=%s", (task_id, operation_id)).fetchone()
            if previous:
                saved = _json(previous["payload"])
                if saved.get("revision", {}).get("input_sha256") != revision_input:
                    raise ValueError("相同操作标识已用于不同修订输入")
                return previous["version"], saved, True
            _validate_revision_envelope(cursor, task_id, envelope)
            version = cursor.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM design_versions "
                                     "WHERE task_id=%s", (task_id,)).fetchone()["version"]
            cursor.execute("INSERT INTO design_versions (task_id,version,request_version,payload,payload_sha256) "
                           "VALUES (%s,%s,%s,%s,%s)",
                           (task_id, version, envelope["request_version"], Jsonb(payload), digest(payload)))
            _event(cursor, task_id, actor or envelope.get("actor", "researcher"), "design_revision_adopted",
                   {"design_version": version, "parent_version": parent_version,
                    "parent_sha256": envelope["parent_sha256"], "input_sha256": revision_input,
                    "changed_paths": preview["changed_paths"]})
        db.commit()
    return version, payload, False


def execution_plan_template(task_id: str, design_version: int) -> dict:
    record = get_design(task_id, design_version)
    task = get_task(task_id)
    if task["active_request_version"] != record["request_version"]:
        raise ValueError("来源设计引用的不是当前已确认请求")
    if get_latest_design_version(task_id) != design_version:
        raise ValueError("来源设计已过期；请从最新版本导出计划模板")
    request = get_request_version(task_id, record["request_version"])
    source = normalize_design_gaps(dict(record["payload"]))
    candidates = [{"id": item["id"], "description": item.get("description", ""),
                  "composition": dict(item.get("composition") or {})}
                 for item in source.get("candidates", [])]
    experiments = []
    for item in source.get("experiments", []):
        candidate_ids = item.get("candidate_ids") or ([candidates[0]["id"]] if candidates else [])
        for candidate_id in candidate_ids:
            experiments.append({"id": f"{item['id']}__{candidate_id}", "source_experiment_id": item["id"],
                                "candidate_id": candidate_id, "observable": item.get("observable", ""),
                                "observable_unit": item.get("unit") or "", "conditions": {}, "method": "",
                                "method_version": "", "model": "", "model_version": "", "parameters": {},
                                "quality_rules": [], "expected_raw_files": [], "max_walltime_minutes": 0,
                                "hypothesis_ids": []})
    paradigm = source.get("paradigm")
    source_ids = [item["id"] for item in candidates]
    pairs = [[source_ids[0], item] for item in source_ids[1:]] if paradigm == "comparison" and source_ids else []
    questions = source.get("open_questions", [])
    return {"schema": "lapis.execution-plan.v2", "request_version": record["request_version"],
            "purpose": request["payload"].get("fields", {}).get("purpose", {}).get("value") or source.get("rationale", ""),
            "paradigm": paradigm, "execution_scope": "research", "candidates": candidates, "experiments": experiments,
            "comparison_pairs": pairs, "screening_criteria": [], "hypothesis_tests": {},
            "budget": {"estimated_resources": {}, "total_runs": 0, "max_walltime_minutes": 0, "scope": ""},
            "gap_dispositions": [{"gap_id": q["id"], "status": "unresolved", "reason": "",
                                  "basis_fields": [], "plan_pointer": ""} for q in questions
                                 if q.get("blocks_execution", True)],
            "provenance": {"request_version": record["request_version"],
                           "request_sha256": request["payload_sha256"],
                           "source_design_version": design_version,
                           "source_design_sha256": record["payload_sha256"].strip(),
                           "operation_id": "填写唯一操作标识"}}


def propose_execution_plan(task_id: str, payload: dict, actor: str) -> tuple[int, dict, bool]:
    if payload.get("schema") != "lapis.execution-plan.v2":
        raise ValueError("执行计划模板契约不正确")
    provenance = payload.get("provenance") or {}
    operation_id = provenance.get("operation_id")
    if not operation_id or operation_id == "填写唯一操作标识":
        raise ValueError("执行计划须设置唯一 operation_id")
    ensure_finite_numbers(payload)
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            current = payload.get("request_version")
            _require_current_request(cursor, task_id, current)
            existing = cursor.execute("SELECT version,payload FROM design_versions WHERE task_id=%s "
                                      "AND payload->'provenance'->>'operation_id'=%s", (task_id, operation_id)).fetchone()
            if existing:
                saved = _json(existing["payload"])
                if digest(saved) != digest(payload):
                    raise ValueError("相同操作标识已用于不同执行计划")
                return existing["version"], saved, True
            source_version = provenance.get("source_design_version")
            latest = cursor.execute("SELECT MAX(version) AS version FROM design_versions WHERE task_id=%s",
                                    (task_id,)).fetchone()["version"]
            if source_version != latest:
                raise ValueError(f"来源设计已不是最新版本；当前为 v{latest}，请重新导出模板")
            next_version = latest + 1
            _check_bound_execution_plan(cursor, task_id, next_version, payload, current)
            cursor.execute("INSERT INTO design_versions (task_id,version,request_version,payload,payload_sha256) "
                           "VALUES (%s,%s,%s,%s,%s)",
                           (task_id, next_version, current, Jsonb(payload), digest(payload)))
            _event(cursor, task_id, actor, "execution_plan_proposed",
                   {"design_version": next_version, "source_design_version": source_version,
                    "payload_sha256": digest(payload), "operation_id": operation_id})
        db.commit()
    return next_version, payload, False


def approve_design(task_id: str, version: int, reviewer: str, statement: str | None = None) -> None:
    """Record a reviewer claim; CLI identity is not authenticated for real execution."""
    if len((reviewer or "").strip()) < 2:
        raise ValueError("审核人声明不能为空")
    if len((statement or "").strip()) < 12:
        raise ValueError("审核时须填写至少 12 个字符的审核声明")
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id FROM research_tasks WHERE id=%s FOR UPDATE", (task_id,))
            if cursor.fetchone() is None:
                raise ValueError("任务不存在")
            cursor.execute("SELECT active_request_version FROM research_tasks WHERE id=%s", (task_id,))
            current = cursor.fetchone()["active_request_version"]
            latest_design = cursor.execute("SELECT MAX(version) AS version FROM design_versions WHERE task_id=%s",
                                           (task_id,)).fetchone()["version"]
            if version != latest_design:
                raise ValueError(f"存在更新的研究设计 v{latest_design}；旧版本不能新审核")
            cursor.execute(
                "SELECT request_version,payload,payload_sha256,status FROM design_versions "
                "WHERE task_id=%s AND version=%s FOR UPDATE",
                (task_id, version),
            )
            row = cursor.fetchone()
            if row is None or row["status"] != "proposed":
                raise ValueError("设计不存在或已审核；请创建新版本并重新审核")
            if row["request_version"] != current or current is None:
                raise ValueError("设计引用了过期研究请求；须重新确认当前请求")
            _require_current_request(cursor, task_id, row["request_version"])
            payload = _json(row["payload"])
            if digest(payload) != row["payload_sha256"].strip():
                raise ValueError("研究计划内容校验和不符，拒绝审核")
            if payload.get("schema") == "lapis.design-draft.v1":
                raise ValueError("模型草案不能直接审批；请先补全为执行计划并另存新版本")
            _check_bound_execution_plan(cursor, task_id, version, payload, row["request_version"])
            cursor.execute(
                "UPDATE design_versions SET status='approved', reviewer=%s, reviewed_at=NOW() WHERE task_id=%s AND version=%s",
                (reviewer, task_id, version),
            )
            _event(cursor, task_id, reviewer, "design_approved", {
                "design_version": version, "payload_sha256": row["payload_sha256"].strip(),
                "request_version": row["request_version"], "statement": statement.strip(),
                "identity_authenticated": False,
            })
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
            active = cursor.execute("SELECT active_request_version FROM research_tasks WHERE id=%s",
                                    (task_id,)).fetchone()["active_request_version"]
            latest_design = cursor.execute("SELECT MAX(version) AS version FROM design_versions WHERE task_id=%s",
                                           (task_id,)).fetchone()["version"]
            if version != latest_design:
                raise ValueError(f"已有更新的设计 v{latest_design}；须先审核最新版本")
            cursor.execute(
                "SELECT request_version,payload,payload_sha256,status,reviewer "
                "FROM design_versions WHERE task_id=%s AND version=%s FOR UPDATE",
                (task_id, version),
            )
            design = cursor.fetchone()
            if design is None or design["status"] != "approved" or design["request_version"] != current or active != current:
                raise ValueError("仅能冻结引用当前请求且经审核的研究设计")
            _require_current_request(cursor, task_id, design["request_version"])
            payload = _json(design["payload"])
            if digest(payload) != design["payload_sha256"]:
                raise ValueError("研究设计内容与保存时校验和不符，拒绝冻结")
            if payload.get("schema") == "lapis.design-draft.v1":
                raise ValueError("模型草案不能冻结；请先补全为执行计划并另存新版本")
            _check_bound_execution_plan(cursor, task_id, version, payload, design["request_version"])
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
            design = cursor.execute("SELECT d.payload FROM design_versions d JOIN execution_requests e "
                                    "ON e.task_id=d.task_id AND e.design_version=d.version WHERE e.id=%s",
                                    (execution_id,)).fetchone()
            if not design or _json(design["payload"]).get("execution_scope") != "software_test":
                raise ValueError("模拟适配器只接受显式标记为 software_test 的执行计划")
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


def record_intake_run(task_id, actor, operation_id, model, prompt_hash, elapsed, outcome, error_type):
    """Failure telemetry does not change draft, task revision or confirmation. Never log credentials."""
    try:
        with connect() as db:
            with db.cursor() as cursor:
                if not cursor.execute("SELECT 1 FROM research_tasks WHERE id=%s", (task_id,)).fetchone():
                    return
                attempt = cursor.execute("SELECT count(*) FROM audit_events WHERE task_id=%s AND action='intake_run' "
                                         "AND details->>'operation_id'=%s", (task_id, operation_id)).fetchone()[0] + 1
                _event(cursor, task_id, actor, "intake_run", {
                    "operation_id": operation_id, "model": model, "model_snapshot": "alias_unpinned",
                    "prompt_version": prompt_hash, "elapsed_seconds": round(elapsed, 3),
                    "attempt": attempt, "outcome": outcome, "error_type": error_type,
                    "sdk_retries": "not_exposed", "usage": "unavailable"})
    except psycopg.Error:
        # A database failure must not replace the original exception; caller still sees failure.
        pass


def get_recent_intake_turns(task_id, limit=4):
    """Original user text and immutable output, without a second history store."""
    with connect() as db:
        with db.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT a.details->>'input' AS input,o.operation_id,o.result FROM intake_operations o JOIN audit_events a ON a.task_id=o.task_id AND a.action='intake_turn' AND (a.details->>'revision')::int=o.revision WHERE o.task_id=%s ORDER BY o.revision DESC LIMIT %s",(task_id,limit))
            return list(reversed(cursor.fetchall()))
