"""Versioned, bounded research guidance; no generated citations or runtime web service."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from lapis_contract import content_hash

CATALOG_PATH = Path(__file__).parent / "data" / "research_directions.json"


def load_catalog(path=None):
    catalog = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    ids = [x["id"] for x in catalog["directions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("推荐方向 ID 重复")
    for direction in catalog["directions"]:
        if not direction.get("source_refs") or any(ref not in catalog["sources"] for ref in direction["source_refs"]):
            raise ValueError("推荐缺少来源")
    return catalog


def recommendations(state, user_text, catalog=None):
    catalog = catalog or load_catalog()
    fields = state["fields"]
    text = " ".join((fields[k].get("value") or "") for k in
                    ("research_object", "purpose", "application")) + " " + user_text
    declined = {x["direction_id"] for x in state.get("recommendation_history", [])
                if x["status"] == "rejected"}
    matches = []
    for direction in catalog["directions"]:
        if direction["id"] in declined:
            continue
        score = sum(alias.casefold() in text.casefold() for alias in direction["aliases"])
        if score:
            matches.append((score, direction))
    matches.sort(key=lambda x: (-x[0], x[1]["id"]))
    # Catalog order resolves ties so the displayed option numbering is reproducible.
    chosen_ids = {x[1]["id"] for x in matches[:3]}
    chosen = [x for x in catalog["directions"] if x["id"] in chosen_ids]
    if not chosen:
        return None
    used = {ref for d in chosen for ref in d["source_refs"]}
    return {"id": str(uuid4()), "version": len(state.get("recommendation_history", [])) + 1,
            "draft_id": state["draft_id"], "catalog_version": catalog["version"],
            "options": deepcopy(chosen),
            "sources": {ref: {**deepcopy(catalog["sources"][ref]),
                              "content_sha256": content_hash(catalog["sources"][ref]),
                              "catalog_version": catalog["version"]} for ref in used}}


def explain_field(field):
    meanings = {
        "application": "应用场景指材料的实际用途或服役背景；不是必须立即选计算方法。",
        "research_object": "研究对象可以是具体分子，也可以是含该基团的材料体系；两者的性质不能直接互相推广。",
        "purpose": "研究目的说明本轮想比较、筛选或探索什么判断。",
        "target_performance": "目标性能可先用定性方向表述，不必先知道具体计算量。",
        "material_function": "材料功能是它在所选用途里承担的作用。",
        "research_scope": "范围说明本轮包含和暂不研究的对象、变化或比较。",
        "constraints": "硬约束必须满足，偏好可以权衡；未知不是没有限制。",
        "work_conditions": "条件未提供可保留未知；说出互不一致的温度或单位则需要澄清。",
    }
    return meanings.get(field, "先限定本轮研究对象、用途和希望作出的判断。")
