"""Agent 2 — Security: chọn Checkov CKV IDs cần enforce cho từng resource.

Menu per resource type grounding LLM về đúng IDs hợp lệ — không hallucinate.
Fail không chặn pipeline: profile rỗng → A4 skip security gate (best-effort deploy).
"""
import json
import logging
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from prompts.security import SYSTEM_PROMPT, USER_TEMPLATE, RETRY_MSG

logger = logging.getLogger(__name__)

_CATALOG_FILE = Path(__file__).parent.parent / "core" / "catalog.json"

def _load_catalog() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """catalog.json → {resource_type → {category → [(id, name)]}}."""
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Không nạp được catalog.json: %s — A2 menu rỗng", e)
        return result
    for rtype, checks in data.items():
        by_cat = result.setdefault(rtype, {})
        for c in checks:
            cid = c.get("id", "")
            name = c.get("name", "")
            for cat in c.get("cat", []):
                by_cat.setdefault(cat, []).append((cid, name))
    return result


_CATALOG: dict[str, dict[str, list[tuple[str, str]]]] = _load_catalog()


@lru_cache(maxsize=None)
def _valid_ids(rtype: str) -> frozenset[str]:
    by_cat = _CATALOG.get(rtype, {})
    return frozenset(cid for entries in by_cat.values() for cid, _ in entries)


@lru_cache(maxsize=None)
def _build_menu(rtype: str) -> str:
    """Render menu text để inject vào prompt. Ví dụ:

      ENCRYPTION:
        CKV_AWS_19: Ensure all data stored in the S3 bucket is securely encrypted at rest
      IAM:
        CKV_AWS_70: Ensure S3 bucket does not allow an action with any Principal
    """
    by_cat = _CATALOG.get(rtype, {})
    if not by_cat:
        return "    (no applicable security checks for this resource type)"
    lines = []
    for cat in sorted(by_cat):
        lines.append(f"    {cat}:")
        for cid, name in sorted(by_cat[cat]):
            lines.append(f"      {cid}: {name}")
    return "\n".join(lines)


def _clean_profile(parsed: dict, resources: list[dict]) -> dict[str, dict]:
    """Normalize LLM output → profile. Drop IDs ngoài menu (hallucination)."""
    out: dict[str, dict] = {}
    for r in resources:
        label = f"{r.get('type')}.{r.get('name')}"
        rtype = r.get("type", "")

        prof = parsed.get(label, {})
        raw_checks = prof.get("checks", []) if isinstance(prof, dict) else []

        valid = _valid_ids(rtype)
        checks = sorted(c for c in raw_checks if isinstance(c, str) and c in valid)

        out[label] = {"type": rtype, "checks": checks}
    return out


def security_node(state: AgentState) -> dict:
    """LangGraph node — chọn security rules cho từng resource trong plan A1."""
    resources = state["infrastructure_plan"].get("resources", [])
    if not resources:
        # Plan thật sự không có resource → không có gì để bảo vệ. KHÔNG phải degraded.
        return {"security_profile": {}, "security_status": "ok"}

    # Dedup menu: cùng type chỉ render 1 lần, liệt kê labels trên header.
    by_type: dict[str, list[str]] = defaultdict(list)
    for r in resources:
        rtype = r.get("type", "")
        by_type[rtype].append(f"{rtype}.{r.get('name')}")
    menu_blocks = [
        f"  {', '.join(labels)}:\n{_build_menu(rtype)}"
        for rtype, labels in by_type.items()
    ]
    menu_str = "\n".join(menu_blocks)

    slim_resources = [{"type": r.get("type"), "name": r.get("name")} for r in resources]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            PROMPT=state["prompt"],
            PLAN=json.dumps({"resources": slim_resources}),
            MENU=menu_str,
        )},
    ]

    raw = ""
    parsed: dict = {}
    for attempt in range(2):
        try:
            raw = call_llm(messages, agent="security")
            parsed = parse_llm_json(raw, {})
            break
        except Exception as e:
            if attempt == 0:
                logger.warning("Security agent retry: %s", e)
                messages = messages + [
                    {"role": "assistant", "content": raw or ""},
                    {"role": "user", "content": RETRY_MSG},
                ]
            else:
                logger.warning("Security agent failed: %s — checks=[] cho mọi resource", e)
                profile = _clean_profile({}, resources)
                return {"security_profile": profile, "security_status": "degraded"}

    if not isinstance(parsed, dict):
        parsed = {}

    profile = _clean_profile(parsed, resources)
    checks_by_res = {lbl: p["checks"] for lbl, p in profile.items()}
    logger.info("Security agent: %d resources | checks=%s", len(profile), checks_by_res)
    return {"security_profile": profile, "security_status": "ok"}
