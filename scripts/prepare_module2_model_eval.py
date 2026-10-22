"""Prepare an offline manifest and conservative call budget for module 2 model eval.

This script intentionally never creates an API client or sends a request.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = [
    {"id": "fec-screening", "initial": "FEC comparison design with unresolved scientific gaps",
     "revision": "researcher supplies a reviewable comparison change", "case": "cases/fec_screening_draft.json"},
    {"id": "comparison-revision", "initial": "two-candidate comparison design",
     "revision": "revise one candidate while preserving the comparison contract", "case": None},
    {"id": "mechanism-verification", "initial": "hypothesis, observable prediction, and falsifier",
     "revision": "revise a test while keeping hypothesis-to-experiment mapping consistent", "case": None},
]
MAX_RETRIES = 2
MAX_OUTPUT_TOKENS = 3200


def prepare() -> dict:
    fec = json.loads((ROOT / SCENARIOS[0]["case"]).read_text(encoding="utf-8"))
    if fec.get("origin") != "demo" or fec.get("execution_allowed") is not False or fec.get("scientific_result") is not None:
        raise ValueError("FEC 场景必须保持演示来源、禁止计算且没有科研结果")
    operations = [
        {"scenario": scenario["id"], "kind": kind, "purpose": purpose}
        for scenario in SCENARIOS
        for kind, purpose in (("initial", scenario["initial"]), ("revision", scenario["revision"]))
    ]
    attempts = len(operations) * (1 + MAX_RETRIES)
    return {
        "mode": "offline-preparation-only",
        "network_calls_made": 0,
        "scenarios": SCENARIOS,
        "logical_operations": len(operations),
        "operations": operations,
        "budget": {
            "max_retries_per_operation": MAX_RETRIES,
            "max_http_attempts": attempts,
            "max_output_tokens_per_attempt": MAX_OUTPUT_TOKENS,
            "max_completion_tokens": attempts * MAX_OUTPUT_TOKENS,
            "input_tokens": "not bounded by max_tokens; capture provider usage before cost approval",
            "currency_cost": "not estimated; depends on selected model and current provider pricing",
        },
        "acceptance_boundary": "Offline self-check validates only this manifest and case boundary; it is not model or scientific acceptance.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true", help="validate scenario manifest without network access")
    args = parser.parse_args()
    manifest = prepare()
    if args.self_check:
        assert manifest["network_calls_made"] == 0
        assert manifest["logical_operations"] == 6
        assert manifest["budget"]["max_http_attempts"] == 18
        assert manifest["budget"]["max_completion_tokens"] == 57_600
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
