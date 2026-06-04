"""Agent 3 — Engineering: JSON plan + security profile → Terraform HCL.

Khi nhận fix_instruction: incremental patch (gửi code cũ + yêu cầu fix) thay vì
rewrite từ đầu — tránh mất các edit security companion từ vòng trước.
Strip <plan> tags (reasoning model chain-of-thought) và preamble text trước HCL.
"""
import json
import logging
import re

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail
from core.parsers import strip_code_block, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.catalog import get_check_names
from prompts.engineering import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT, USER_TEMPLATE as _USER_TEMPLATE,
    PATCH_HEADER, PREV_CODE_HEADER, PREV_ERRORS_HEADER, NO_RESOURCE_RETRY,
)

logger = logging.getLogger(__name__)

_CKV_NAME: dict[str, str] = get_check_names()

# Xóa <plan>...</plan> tags từ LLM output.
# Reasoning model (deepseek-v4-pro) đôi khi wrap chain-of-thought trong <plan> tags
# trước khi trả HCL — những tags này không phải HCL hợp lệ, phải strip ra.
_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)

# _RESOURCE_DECL_RE (`resource "type" "name"`, group1=type, group2=name) import từ
# core.parsers — dùng để log và đếm resource count; dùng chung với A4.

# Các keyword bắt đầu một block HCL hợp lệ.
# Dùng để tìm điểm đầu tiên cần giữ trong output LLM (strip preamble text trước đó).
_HCL_BLOCK_START = re.compile(r'(?:terraform\s*\{|provider\s+"|resource\s+"|data\s+"|variable\s+"|output\s+"|module\s+")')


def _strip_preamble(hcl: str) -> str:
    """Bỏ phần văn bản LLM viết trước block HCL đầu tiên.

    LLM thường viết intro như "Here's the Terraform configuration:" trước block code.
    Những text này không phải HCL → terraform validate sẽ fail.

    Ví dụ:
      Input:  "Sure! Here's the config:\n\nresource \"aws_s3_bucket\" \"main\" { ... }"
      Output: "resource \"aws_s3_bucket\" \"main\" { ... }"
    """
    m = _HCL_BLOCK_START.search(hcl)
    return hcl[m.start():] if m else hcl


def _clean_hcl(raw: str) -> str:
    """Clean LLM output thành HCL thuần.

    3 bước theo thứ tự:
      1. Xóa <plan>...</plan> tags (reasoning model chain-of-thought)
      2. Xóa ```hcl...``` markdown fence
      3. Xóa text giải thích trước block HCL đầu tiên
    """
    cleaned = _PLAN_TAG.sub("", raw).strip()
    return _strip_preamble(strip_code_block(cleaned).strip())


def engineering_node(state: AgentState) -> dict:
    sec_lines = []
    for label, info in state["security_profile"].items():
        checks = info.get("checks", [])
        if not checks:
            continue
        sec_lines.append(f"  {label}:")
        for cid in checks:
            name = _CKV_NAME.get(cid, cid)
            sec_lines.append(f"    - {cid}: {name}")
    ctx_lines = "\n".join(sec_lines) or "  (no security checks selected)"

    user_content = _USER_TEMPLATE.format(
        PLAN=json.dumps(state["infrastructure_plan"]),
        SECURITY_CONTEXT=ctx_lines,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        fix_msg = PATCH_HEADER + fix_instruction
        if state["generated_code"]:
            fix_msg += PREV_CODE_HEADER + state["generated_code"]
        past = [
            e.get("fix_instruction", "")[:200]
            for e in state["eng_error_history"][-2:]
            if e.get("fix_instruction") and e.get("fix_instruction") != fix_instruction
        ]
        if past:
            fix_msg += PREV_ERRORS_HEADER + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})

    raw = ""
    try:
        raw = call_llm(messages, agent="engineering")
    except TimeoutError as e:
        logger.error("Engineering agent timeout: %s", e)
        return make_fail("INFRASTRUCTURE", None, f"Engineering agent LLM timeout: {e}")
    except Exception as e:
        logger.error("Engineering agent error: %s", e)
        return make_fail("INFRASTRUCTURE", None, f"Engineering agent error: {e}")

    body = _clean_hcl(raw)
    if 'resource "' not in body:
        logger.warning("Engineering agent: không có resource block — retry")
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": NO_RESOURCE_RETRY},
        ]
        try:
            raw = call_llm(retry_msgs, agent="engineering")
        except Exception as e:
            return make_fail("INFRASTRUCTURE", None, f"Engineering agent retry error: {e}")
        body = _clean_hcl(raw)
        if 'resource "' not in body:
            return make_fail(
                "INFRASTRUCTURE", None,
                f"Engineering agent không sinh được resource block (sau retry). Raw: {raw[:300]}",
            )

    generated_code = f"{body}\n"
    gen_pairs = set(_RESOURCE_DECL_RE.findall(body))
    logger.info("Engineering agent: %d chars, %d resources", len(generated_code), len(gen_pairs))

    out: dict = {"generated_code": generated_code, "fix_feedback": {}}
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        out["eng_error_history"] = (state["eng_error_history"] + [{"fix_instruction": fix_instruction}])[-5:]
    return out
