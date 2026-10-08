"""Compare Jev and DeepSeek intent-classification HTTP phases without logging keys."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


TEXTS = [
    "筛选出锂离子扩散系数最高的电解液",
    "比较材料A和材料B的吸附能差异",
    "验证FEC是否通过改变溶剂化结构提升循环寿命",
    "看看FEC为什么能提升循环寿命",
    "我想研究电解液",
    "找到耐热性最好的呋喃材料",
    "比较两种催化剂的反应势垒",
    "验证催化剂是否通过增强氧气吸附促进反应",
    "研究催化剂的反应路径",
    "越多越好",
]

CRITERIA = {
    "screening": "选出或推荐表现最好的候选材料",
    "comparison": "比较指定材料之间的差异，不要求选出最优",
    "mechanism_validation": "检验一个已提出的具体作用机制是否成立",
    "mechanism_exploration": "探索原因或反应路径，尚无待检验的具体机制主张",
    "unknown": "无法根据现有话语判断材料研究任务目的",
}

DEEPSEEK_SYSTEM = (
    "只判断材料研究任务的目的，返回JSON对象，只有task_type字段。"
    "可选值：screening=筛选最优候选；comparison=比较差异；"
    "mechanism_validation=检验明确机制假设；mechanism_exploration=探索未知原因或路径；"
    "unknown=无法判断。不要解释。"
)


def payload(provider: str, text: str) -> dict:
    if provider == "jev":
        return {
            "model": "jev-latest", "state": text,
            "questions": {"task_type": {
                "type": "choice", "instructions": "用户最终想得到什么研究结论？",
                "criteria": CRITERIA,
            }},
        }
    return {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": DEEPSEEK_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0, "max_tokens": 80,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }


def measure_batch(provider: str, key: str, round_number: int) -> list[dict]:
    url = ("https://api.typesafe.ai/v1/systemone" if provider == "jev"
           else "https://api.deepseek.com/chat/completions")
    with tempfile.TemporaryDirectory(prefix="lapis_latency_") as folder:
        config = []
        for index, user_text in enumerate(TEXTS):
            body = Path(folder) / f"request_{index}.json"
            body.write_text(json.dumps(payload(provider, user_text), ensure_ascii=False), encoding="utf-8")
            if index:
                config.append("next")
            config.extend([
                f'url = "{url}"',
                'request = "POST"',
                'silent', 'show-error', 'max-time = 40',
                'output = "NUL"',
                'header = "Content-Type: application/json"',
                f'header = "Authorization: Bearer {key}"',
                f'data-binary = "@{body.as_posix()}"',
                f'write-out = "LAPIS {provider} {round_number} {index} '
                '%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} '
                '%{time_pretransfer} %{time_starttransfer} %{time_total} %{size_download}\\n"',
            ])
        run = subprocess.run(
            ["curl.exe", "--config", "-"], input="\n".join(config) + "\n",
            text=True, capture_output=True, timeout=450,
        )
    if run.returncode:
        raise RuntimeError(f"{provider} curl exit {run.returncode}: {run.stderr[:300]}")
    rows = []
    for line in run.stdout.splitlines():
        if not line.startswith("LAPIS "):
            continue
        _, name, round_text, index_text, code, dns, tcp, tls, pre, first, total, size = line.split()
        if code != "200":
            raise RuntimeError(f"{name} request {index_text} returned HTTP {code}")
        rows.append({
            "provider": name, "round": int(round_text), "index": int(index_text),
            "dns_ms": 1000 * float(dns), "tcp_ms": 1000 * float(tcp),
            "tls_ms": 1000 * float(tls), "pretransfer_ms": 1000 * float(pre),
            "first_byte_ms": 1000 * float(first), "total_ms": 1000 * float(total),
            "wait_after_setup_ms": 1000 * (float(first) - float(pre)),
            "download_ms": 1000 * (float(total) - float(first)),
            "response_bytes": int(size),
        })
    if len(rows) != len(TEXTS):
        raise RuntimeError(f"Expected {len(TEXTS)} rows for {provider}, got {len(rows)}")
    return rows


def summary(rows: list[dict], provider: str) -> dict:
    warm = [row for row in rows if row["provider"] == provider and row["index"] > 0]
    return {
        key: {"mean": round(statistics.mean(row[key] for row in warm), 1),
              "median": round(statistics.median(row[key] for row in warm), 1),
              "min": round(min(row[key] for row in warm), 1),
              "max": round(max(row[key] for row in warm), 1)}
        for key in ("pretransfer_ms", "first_byte_ms", "total_ms", "wait_after_setup_ms", "download_ms")
    }


def main() -> None:
    keys = {"jev": os.getenv("TYPESAFE_API_KEY"), "deepseek": os.getenv("DEEPSEEK_API_KEY")}
    if not all(keys.values()):
        raise SystemExit("TYPESAFE_API_KEY and DEEPSEEK_API_KEY are required")
    rows = []
    for round_number, order in enumerate((("jev", "deepseek"), ("deepseek", "jev")), 1):
        for provider in order:
            rows.extend(measure_batch(provider, keys[provider], round_number))
            print(f"completed {provider} round {round_number}", file=sys.stderr, flush=True)
    result = {"samples_per_provider": 2 * len(TEXTS),
              "warm_samples_per_provider": 2 * (len(TEXTS) - 1),
              "jev": summary(rows, "jev"), "deepseek": summary(rows, "deepseek"),
              "raw": rows}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
