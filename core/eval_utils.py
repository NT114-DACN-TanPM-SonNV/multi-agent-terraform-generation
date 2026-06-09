"""Shared helpers for `evaluate.py` and `trace.py`."""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile


R = "[0m"
BOLD = "[1m"
DIM = "[2m"


def _c(code: str, s: str) -> str:
    return f"{code}{s}{R}"


def bold(s: str) -> str:
    return _c(BOLD, s)


def dim(s: str) -> str:
    return _c(DIM, s)


def green(s: str) -> str:
    return _c("[92m", s)


def red(s: str) -> str:
    return _c("[91m", s)


def yellow(s: str) -> str:
    return _c("[93m", s)


def blue(s: str) -> str:
    return _c("[94m", s)


def magenta(s: str) -> str:
    return _c("[95m", s)


def cyan(s: str) -> str:
    return _c("[96m", s)


def white(s: str) -> str:
    return _c("[97m", s)


def load_dataset_rows(csv_path: Path, limit: int | None) -> list[tuple[int, str, str, list[str]]]:
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]
    result = []
    for i, row in enumerate(rows):
        gt_raw = row.get("Resource") or ""
        gt_types = [t.strip() for t in gt_raw.split(",") if t.strip()]
        result.append((i, row.get("Difficulty", ""), row["Prompt"], gt_types))
    return result


def parse_cases(tokens: list[str]) -> set[int]:
    result = set()
    for part in tokens:
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))
    return result


def load_trace_prompt(csv_path: Path, row_index: int) -> tuple[str, str]:
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if row_index < 0 or row_index >= len(rows):
        raise SystemExit(f"CSV row out of range: {row_index} (n={len(rows)})")
    row = rows[row_index]
    prompt = (row.get("Prompt") or "").strip()
    if not prompt:
        raise SystemExit(f"CSV row {row_index} has empty Prompt")
    return prompt, row.get("Difficulty", "?")


def load_existing_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[resume] cannot read existing output {path}: {type(e).__name__}: {e}")
        return []
    if not isinstance(data, list):
        print(f"[resume] existing output is not a list, ignoring: {path}")
        return []
    good = [r for r in data if isinstance(r, dict) and isinstance(r.get("row"), int)]
    if len(good) != len(data):
        print(f"[resume] ignored {len(data) - len(good)} invalid existing result item(s)")
    return good


def atomic_write_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sorted(data, key=lambda r: r["row"]), indent=2, ensure_ascii=False)
    with NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def resource_comparison(gt_types: list[str], created: list[str]) -> dict:
    def _normalize(items: list[str]) -> set[str]:
        out = set()
        for item in items:
            parts = item.split(".")
            if parts[0] == "data" and len(parts) >= 2:
                out.add(parts[1])
            elif parts:
                out.add(parts[0])
        return out

    gt_set = set(gt_types)
    gen_set = _normalize(created)
    tp = gt_set & gen_set
    fp = gen_set - gt_set
    fn = gt_set - gen_set
    precision = len(tp) / len(gen_set) if gen_set else 0.0
    recall = len(tp) / len(gt_set) if gt_set else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "gt": sorted(gt_set),
        "generated": sorted(gen_set),
        "tp": sorted(tp),
        "fp": sorted(fp),
        "fn": sorted(fn),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    }


def check_aws_identity() -> None:
    import boto3
    from botocore.config import Config

    attempts = int(os.environ.get("AWS_PREFLIGHT_RETRIES", "3"))
    cfg = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
        proxies={},
    )
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            ident = boto3.client("sts", config=cfg).get_caller_identity()
            acct = ident.get("Account", "")
            arn = ident.get("Arn", "")
            print(f"AWS identity OK  |  account={acct}  |  arn={arn}")
            return
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                wait = 5 * (i + 1)
                print(
                    f"AWS preflight attempt {i+1}/{attempts} failed "
                    f"({type(e).__name__}) — retry sau {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
    raise RuntimeError(
        "AWS credential preflight failed sau "
        f"{attempts} lần. Terraform plan/apply cũng sẽ fail: "
        f"{type(last_err).__name__}: {last_err}"
    ) from last_err
