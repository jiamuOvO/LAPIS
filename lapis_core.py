"""Small, deterministic gates between intake, research design, and execution."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal
import unicodedata

from pydantic import BaseModel, Field, model_validator
from lapis_contract import validate_research_request_v3, validate_research_request_v4, validate_research_request_v5


class Candidate(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    composition: dict[str, str] = Field(min_length=1)


UNIT_DIMENSIONS = {
    "k": "temperature", "kelvin": "temperature", "°c": "temperature", "degc": "temperature",
    "celsius": "temperature", "°f": "temperature", "degf": "temperature", "fahrenheit": "temperature",
    "pa": "pressure", "kpa": "pressure", "mpa": "pressure", "gpa": "pressure",
    "bar": "pressure", "atm": "pressure",
    "s": "time", "sec": "time", "ms": "time", "us": "time", "µs": "time",
    "ns": "time", "ps": "time", "fs": "time",
    "m": "length", "cm": "length", "mm": "length", "um": "length", "µm": "length",
    "nm": "length", "å": "length", "angstrom": "length",
    "m^2": "area", "m2": "area", "cm^2": "area", "cm2": "area",
    "nm^2": "area", "nm2": "area", "å^2": "area", "å2": "area",
    "m^3": "volume", "m3": "volume", "cm^3": "volume", "cm3": "volume",
    "nm^3": "volume", "nm3": "volume", "å^3": "volume", "å3": "volume",
    "m/s": "velocity", "nm/ns": "velocity", "m^2/s": "diffusion", "m2/s": "diffusion",
    "cm^2/s": "diffusion", "cm2/s": "diffusion", "nm^2/ns": "diffusion",
    "nm2/ns": "diffusion", "å^2/ps": "diffusion", "å2/ps": "diffusion",
    "mol/l": "concentration", "mol/liter": "concentration", "mmol/l": "concentration",
    "mol/m^3": "concentration",
    "kg/m^3": "density", "g/cm^3": "density", "g/ml": "density",
    "j": "energy", "kj/mol": "molar_energy", "kcal/mol": "molar_energy", "ev": "energy",
    "kg": "mass", "g": "mass", "mg": "mass",
    "1": "dimensionless", "%": "fraction", "wt%": "mass_fraction", "mass%": "mass_fraction",
    "mol%": "mole_fraction", "mole%": "mole_fraction",
    "v": "voltage", "mv": "voltage", "a": "current", "ma": "current",
    "s/m": "conductivity", "ms/cm": "conductivity", "c": "charge", "e": "charge",
    "hz": "frequency", "khz": "frequency", "mhz": "frequency",
}


def _unit_key(unit: str) -> str:
    value = unicodedata.normalize("NFKC", unit).strip().casefold()
    return value.replace("²", "^2").replace("³", "^3").replace(" ", "")


def _unit_dimension(unit: str) -> str | None:
    exact = unicodedata.normalize("NFKC", unit).strip().replace(" ", "")
    if exact in {"M", "mM"}:
        return "concentration"
    if exact in {"A", "mA"}:
        return "current"
    key = _unit_key(unit)
    # NFKC normalizes μ to Greek mu; accept both common micro prefixes.
    key = key.replace("μ", "µ")
    return UNIT_DIMENSIONS.get(key)


def _named_dimension(name: str) -> str | None:
    value = unicodedata.normalize("NFKC", name).casefold().replace("_", " ").replace("-", " ").strip()
    groups = {
        "temperature": ("temperature", "temp", "温度", "摄氏", "开尔文"),
        "pressure": ("pressure", "压力"),
        "time": ("time step", "timestep", "duration", "time", "时长", "时间"),
        "length": ("length", "distance", "displacement", "位移", "长度", "尺寸"),
        "area": ("area", "面积"), "volume": ("volume", "体积"),
        "velocity": ("velocity", "速度"),
        "diffusion": ("diffusion coefficient", "diffusivity", "diffusion", "扩散系数", "自扩散"),
        "concentration": ("concentration", "molarity", "浓度"),
        "density": ("mass density", "density", "密度"),
        "molar_energy": ("molar energy", "摩尔能"), "energy": ("energy", "能量"),
        "mass_fraction": ("mass fraction", "质量分数", "wt%"),
        "mole_fraction": ("mole fraction", "摩尔分数", "mol%"),
        "mass": ("mass", "质量"),
        "voltage": ("voltage", "电压"), "current": ("current", "电流"),
        "conductivity": ("conductivity", "电导率", "电导"),
        "charge": ("charge", "电荷"), "frequency": ("frequency", "频率"),
    }
    matches = []
    for dimension, aliases in groups.items():
        for alias in aliases:
            if re.search(r"[a-z0-9]", alias):
                found = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", value)
            else:
                found = alias in value
            if found:
                matches.append((len(alias), dimension, alias))
    if value == "t":
        return "temperature"
    if not matches:
        return None
    specificity = max(length for length, _, _ in matches)
    best = [(dimension, alias) for length, dimension, alias in matches if length == specificity]
    dimensions = {dimension for dimension, _ in best}
    if len(dimensions) != 1:
        return None
    dimension, alias = best[0]
    if any(other_dimension != dimension and other_alias not in alias and alias not in other_alias
           for _, other_dimension, other_alias in matches):
        return None
    return dimension


class Quantity(BaseModel):
    value: float
    unit: str = Field(min_length=1)
    dimension: str | None = None
    unit_review: str | None = Field(default=None, min_length=8)

    @model_validator(mode="after")
    def valid_value_and_unit(self):
        if not math.isfinite(self.value):
            raise ValueError("数值必须是有限值")
        unit_dimension = _unit_dimension(self.unit)
        if unit_dimension:
            if self.dimension and self.dimension != unit_dimension:
                raise ValueError(f"单位 {self.unit} 属于 {unit_dimension} 量纲，与声明的 {self.dimension} 不符")
        elif not self.dimension or not (self.unit_review or "").strip():
            raise ValueError("未知单位须填写量纲和明确的 unit_review 审核说明")
        if unit_dimension == "temperature" or self.dimension == "temperature":
            kelvin = self.value
            key = _unit_key(self.unit)
            if key in {"°c", "degc", "celsius"}:
                kelvin += 273.15
            elif key in {"°f", "degf", "fahrenheit"}:
                kelvin = (kelvin - 32) * 5 / 9 + 273.15
            elif unit_dimension != "temperature":
                raise ValueError("温度只能使用支持换算的 K、°C 或 °F 单位")
            if kelvin <= 0:
                raise ValueError("绝对温度必须高于 0 K")
        return self


class Experiment(BaseModel):
    id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    observable: str = Field(min_length=1)
    observable_unit: str = Field(min_length=1)
    observable_dimension: str | None = None
    observable_unit_review: str | None = Field(default=None, min_length=8)
    conditions: dict[str, Quantity] = Field(min_length=1)
    method: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    parameters: dict[str, str] = Field(min_length=1)
    quality_rules: list[str] = Field(min_length=1)
    expected_raw_files: list[str] = Field(min_length=1)
    max_walltime_minutes: int = Field(gt=0)


class ExecutionPlan(BaseModel):
    """A reviewed proposal. This does not dispatch a job by itself."""

    request_version: int = Field(gt=0)
    purpose: str = Field(min_length=1)
    candidates: list[Candidate] = Field(min_length=1)
    experiments: list[Experiment] = Field(min_length=1)
    source: Literal["researcher"] = "researcher"

    @model_validator(mode="after")
    def references_exist(self):
        ids = [candidate.id for candidate in self.candidates]
        experiment_ids = [experiment.id for experiment in self.experiments]
        if len(ids) != len(set(ids)) or len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError("候选或实验 ID 重复")
        if any(experiment.candidate_id not in ids for experiment in self.experiments):
            raise ValueError("实验引用了不存在的候选")
        for experiment in self.experiments:
            observable_unit_dimension = _unit_dimension(experiment.observable_unit)
            observable_name_dimension = _named_dimension(experiment.observable)
            declared = experiment.observable_dimension
            if observable_unit_dimension:
                if declared and declared != observable_unit_dimension:
                    raise ValueError("观察量声明的量纲与单位不一致")
                if observable_name_dimension and observable_name_dimension != observable_unit_dimension:
                    raise ValueError("观察量名称与单位量纲不一致")
                if not observable_name_dimension and not declared:
                    raise ValueError("无法识别观察量名称；请显式填写 observable_dimension")
            elif not declared or not (experiment.observable_unit_review or "").strip():
                raise ValueError("未知观察量单位须填写量纲和明确的 observable_unit_review 审核说明")
            elif observable_name_dimension and declared != observable_name_dimension:
                raise ValueError("观察量名称与人工声明量纲不一致")
            for name, quantity in experiment.conditions.items():
                expected = _named_dimension(name)
                known = _unit_dimension(quantity.unit)
                declared = quantity.dimension
                actual = declared or known
                if expected and actual != expected:
                    raise ValueError(f"条件 {name} 的单位量纲应为 {expected}")
                if not expected and not declared:
                    raise ValueError(f"无法识别条件 {name} 的量纲；请显式填写 dimension")
                if known and declared and known != declared:
                    raise ValueError(f"条件 {name} 的单位与声明量纲不一致")
        def check(value):
            if isinstance(value, str) and (not value.strip() or any(
                marker in value.casefold() for marker in ("待定", "待确认", "tbd", "todo", "unknown", "test-only")
            )):
                raise ValueError("执行设计包含占位或未定内容")
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            if isinstance(value, list):
                for item in value:
                    check(item)
        check(self.model_dump())
        return self


def canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as error:
        raise ValueError("JSON 数值必须是有限值") from error


def ensure_finite_numbers(value: object, path: str = "研究设计") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}中的数值必须是有限值")
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_finite_numbers(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            ensure_finite_numbers(item, f"{path}[{index}]")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


REQUEST_FIELDS_V2 = {
    "purpose", "research_object", "application", "work_conditions",
    "target_performance", "constraints", "research_scope", "material_function",
}


def validate_research_request_v2(payload: dict) -> None:
    """A confirmed request may enter research design; it never authorizes computation."""
    if not isinstance(payload, dict) or payload.get("contract_version") != 2:
        raise ValueError("进入研究设计前须重新确认 v2 研究请求")
    if not isinstance(payload.get("original_intent"), str) or not payload["original_intent"].strip():
        raise ValueError("研究请求缺少用户原始意图")
    if payload.get("task_pattern") == "out_of_scope":
        raise ValueError("非材料研究不在首版范围")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or set(fields) != REQUEST_FIELDS_V2:
        raise ValueError("研究请求必须分别包含八项输出")
    for key in REQUEST_FIELDS_V2:
        entries = fields[key] if key in {"target_performance", "constraints"} else [fields[key]]
        if not isinstance(entries, list) or not entries or not all(isinstance(x, dict) for x in entries):
            raise ValueError(f"{key} 缺少可检查的状态")
        for entry in entries:
            if entry.get("status") not in {"specified", "none", "open", "unclear", "unknown"}:
                raise ValueError(f"{key} 状态无效")
            source = entry.get("source")
            if source == "system_suggestion":
                raise ValueError(f"{key} 的系统建议尚未获得用户确认")
            if source in {"user", "confirmed_suggestion"} and (
                not isinstance(entry.get("turn"), int) and source == "user"
                or source == "user" and not entry.get("quote")
                or source == "confirmed_suggestion" and not isinstance(entry.get("confirmed_turn"), int)
            ):
                raise ValueError(f"{key} 缺少来源或确认轮次")
    for key in ("purpose", "research_object", "application", "research_scope"):
        entry = fields[key]
        if entry.get("status") != "specified" or not entry.get("value") or entry.get("source") != "user":
            raise ValueError(f"{key} 尚不足以界定研究任务")
    function = fields["material_function"]
    if function.get("status") != "specified" or not function.get("value") or function.get("source") not in {
        "user", "confirmed_suggestion"
    }:
        raise ValueError("材料功能尚未确认")
    goals = fields["target_performance"]
    if any(goal.get("status") == "unclear" or
           goal.get("status") == "specified" and (not goal.get("value") or not goal.get("direction"))
           for goal in goals):
        raise ValueError("目标性能含糊或缺少方向")
    if not any(goal.get("status") == "specified" and goal.get("value") and goal.get("direction")
               and goal.get("source") in {"user", "confirmed_suggestion"} for goal in goals):
        raise ValueError("目标性能方向尚未确认")
    if any(item.get("status") == "unclear" for item in fields["constraints"]):
        raise ValueError("含糊的约束须先澄清")


ATTEMPT_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled", "needs_reconciliation"},
    "needs_reconciliation": {"running", "failed", "cancelled"},
    "succeeded": set(), "failed": set(), "cancelled": set(),
}


def check_attempt_transition(current: str, target: str) -> None:
    if target not in ATTEMPT_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法作业状态转换：{current} → {target}")


def intake_is_confirmed(state: dict, result: dict) -> bool:
    """Only explicit review confirmation creates a versioned research request."""
    request = result.get("request")
    if isinstance(request, dict) and request.get("contract_version") in {3, 4, 5}:
        try:
            {3:validate_research_request_v3,4:validate_research_request_v4,5:validate_research_request_v5}[request["contract_version"]](request)
        except ValueError:
            return False
        return bool(result.get("ready_for_design") and (result.get("confirmation_event") or result.get("already_confirmed"))
                    and result.get("calculation_status") == "pending_research_design"
                    and state.get("stage") == "ready_for_design"
                    and state.get("draft_id") == request.get("draft_id"))
    if isinstance(request, dict) and request.get("contract_version") == 2:
        try:
            validate_research_request_v2(request)
        except ValueError:
            return False
        return bool(result.get("ready_for_design") and result.get("confirmation_event")
                    and result.get("calculation_status") == "pending_research_design"
                    and state.get("stage") == "ready_for_design" and state.get("turns")
                    and state["turns"][-1] in {"确认", "确认继续", "按此继续", "就按这个", "同意"})
    # Historical v1 requests stay readable. New design proposals reject them until v2 reconfirmation.
    return bool(result.get("ready") and result.get("calculation_status") == "pending_research_design"
                and state.get("stage") is None and state.get("turns")
                and state["turns"][-1] in {"确认", "确认继续", "按此继续", "就按这个", "同意", "采用方案"})


def verify_artifact(path: Path, root: Path) -> tuple[str, int, str]:
    """Hash an existing regular file under the controlled artifact directory."""
    root = root.resolve(strict=True)
    path = path.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("产物必须是受控目录内的普通文件")
    before = path.stat()
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("产物在计算哈希期间被修改")
    return path.relative_to(root).as_posix(), after.st_size, hasher.hexdigest()
