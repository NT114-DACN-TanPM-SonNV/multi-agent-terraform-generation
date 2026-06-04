"""Agent 4 — Validation: terraform init → validate → plan → Checkov gate.

Error types: SYNTAX (validate) | LOGIC/MISSING_RESOURCE (plan, hybrid pattern+LLM)
| SECURITY (Checkov, best-effort nếu hết budget) | INFRASTRUCTURE (timeout/auth).
Checkov scan trên plan JSON (terraform show -json) — chính xác hơn source scan.
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.catalog import get_check_names
from core.terraform import (
    run_terraform, write_terraform_dir, terraform_workdir,
    run_checkov_on_hcl, run_checkov_on_plan, tf_init_cmd,
)
from core.retry_control import (
    increment_retry, check_retry_budget,
    MAX_TOTAL_RETRY, MAX_VAL_ENG_RETRY, MAX_VAL_ARCH_RETRY, MAX_VAL_SEC_RETRY,
)
from core.errors import matches_any, MISSING_RESOURCE_PATTERNS, AUTH_PATTERNS
from prompts.validation import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM,
    INIT_FIX, PLAN_CONTEXT, SECURITY_FIX,
)

logger = logging.getLogger(__name__)

_INIT_TIMEOUT     = 300
_VALIDATE_TIMEOUT = 30
_SHOW_TIMEOUT     = 15   # local file, no network
# Ghi sau init thành công. Retry thấy marker → skip init (5-10s per cycle).
# Không ghi khi fail → partial .terraform/ không bị reuse.
_INIT_MARKER = ".tf_init_done"


# ── Security gate catalog ──────────────────────────────────────────────────────
# A2 chọn trực tiếp CKV IDs per resource (grounded bằng catalog menu).
#   - _targets_for_plan chỉ đọc profile["checks"] — không cần level/tier trung gian
#   - get_check_names() render fix_instruction human-readable cho A3 (id → tên check)
#
# Nguồn tên check: core/catalog.json (sinh bởi core/build_catalog.py).
_CKV_NAME: dict[str, str] = get_check_names()


def _targets_for_plan(profile: dict) -> tuple[set[str], dict[str, set[str]]]:
    """Từ security_profile A2 → tập CKV ID cần verify, toàn cục + theo từng resource addr.

    A2 đã chọn trực tiếp CKV IDs per resource (grounded bằng catalog menu).
      - Hàm này chỉ đọc profile["checks"] và build per_res + global_ids
      - Không có level/tier trung gian: profile là nguồn target duy nhất

    Returns:
        global_ids: tập tất cả IDs cần pass vào run_checkov_on_hcl
        per_res:    {resource_addr → set(ids)} — để _enforceable_unmet biết
                    check nào là bắt buộc cho resource cụ thể nào
    """
    per_res: dict[str, set[str]] = {}
    global_ids: set[str] = set()
    for addr, info in (profile or {}).items():
        ids = set(info.get("checks", []))
        if ids:
            per_res[addr] = ids
            global_ids.update(ids)
    return global_ids, per_res


# Patterns phân loại lỗi terraform plan (tất định, không cần LLM).
# Tại sao tách TRANSIENT và AUTH?
#   TRANSIENT: lỗi mạng tạm thời (connection reset, throttle) → retry plan trong node
#   AUTH: lỗi credential/provider → không retry (sửa code vô nghĩa, cần AWS setup)
#   INFRA = TRANSIENT ∪ AUTH: cả hai đều route requires_human sau khi xử lý xong
#
# Tại sao không dùng bare "timeout"?
#   aws_db_instance có attribute `timeout {}` block — bare "timeout" sẽ false-positive.
#   Dùng cụm cụ thể: "i/o timeout", "context deadline exceeded", "timed out".
_PLAN_TRANSIENT_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "i/o timeout", "dial tcp", "no such host", "context deadline exceeded",
    "tls handshake timeout", "requesttimeout",
    "throttling", "requestlimitexceeded", "rate exceeded", "limitexceeded",
)
_PLAN_AUTH_PATTERNS = AUTH_PATTERNS   # canonical set từ core.errors — sync với A5
_PLAN_INFRA_PATTERNS = _PLAN_TRANSIENT_PATTERNS + _PLAN_AUTH_PATTERNS

_MAX_PLAN_TRANSIENT_RETRY = 1
_PLAN_RETRY_BACKOFF = 3


def _hcl_resource_labels(code: str) -> list[str]:
    """Trích list "type.name" từ HCL code — cung cấp context cho LLM classify."""
    return [f"{t}.{n}" for t, n in _RESOURCE_DECL_RE.findall(code)]


def _extract_code_context(validate_err: str, code: str, window: int = 4,
                          max_errors: int = 6) -> str:
    """Trích lines xung quanh mỗi dòng lỗi, đánh dấu ">>>". finditer để lấy hết
    tất cả lỗi một lần — A3 sửa được hết trong 1 vòng thay vì whack-a-mole.
    """
    line_nums: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"on main\.tf line (\d+)", validate_err):
        ln = int(m.group(1))
        if ln not in seen:
            seen.add(ln)
            line_nums.append(ln)
        if len(line_nums) >= max_errors:
            break
    if not line_nums:
        return ""
    lines = code.split("\n")
    blocks = []
    for line_num in line_nums:
        start = max(0, line_num - window - 1)
        end   = min(len(lines), line_num + window)
        parts = []
        for i, ln in enumerate(lines[start:end], start=start + 1):
            marker = ">>>" if i == line_num else "   "
            parts.append(f"{i:3d} {marker} {ln}")
        blocks.append("\n".join(parts))
    return "\n---\n".join(blocks)


def _success_result(checkov: dict, unmet: list | None = None,
                    phantom: list | None = None,
                    security_degraded: bool = False) -> dict:
    # unmet không block: impossible checks (companion resource không được thêm) sẽ
    # stuck pipeline mà không có deliverable. Best-effort + báo cáo tốt hơn block cứng.
    return {
        "fix_feedback": {
            "overall_passed": True, "error_type": None, "root_cause": None,
            "fix_instruction": None, "checkov": checkov,
            "unmet_checks": [{"resource": a, "ckv_id": i, "name": n} for a, i, n in (unmet or [])],
            "phantom_checks": list(phantom or []),
            "security_degraded": security_degraded,
            "validate_passed": True, "plan_passed": True,
        },
    }


def _enforceable_unmet(per_res: dict[str, set[str]], checkov: dict) -> list[tuple[str, str, str]]:
    """Intersection: check vừa fail (Checkov) vừa được A2 target cho resource đó.
    Checkov có thể báo cùng (addr, id) nhiều lần → dedup.
    """
    unmet: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for addr, ckv_id in checkov.get("failed_per_resource", []):
        if ckv_id not in per_res.get(addr, ()):
            continue
        key = (addr, ckv_id)
        if key in seen:
            continue
        seen.add(key)
        unmet.append((addr, ckv_id, _CKV_NAME.get(ckv_id, ckv_id)))
    return unmet


def _security_return(state: AgentState, unmet: list[tuple[str, str, str]],
                     checkov: dict, phantom: list | None = None) -> dict:
    """Checkov fail + còn budget → route SECURITY về A3. fix_instruction dùng tên check
    (không chỉ ID) để A3 hiểu được cần implement gì.
    """
    new_total = state["total_val_attempts"] + 1
    increment_retry(state, "sec", "SECURITY", str(sorted({cid for _a, cid, _n in unmet})))
    fix_instruction = SECURITY_FIX.format(
        items="\n".join(f"- {addr}: {name}" for addr, _id, name in unmet)
    )
    signature = sorted({cid for _a, cid, _n in unmet})
    logger.info("Agent 4: FAIL SECURITY — %d unmet check(s): %s", len(unmet), signature)
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "SECURITY", "root_cause": "engineering",
            "fix_instruction": fix_instruction, "raw_error": "",
            "checkov": checkov,
            "unmet_checks": [{"resource": a, "ckv_id": i, "name": n} for a, i, n in unmet],
            "phantom_checks": list(phantom or []),
            "validate_passed": True, "plan_passed": True,
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "SECURITY", "root_cause": "engineering",
            "fix_instruction": fix_instruction, "predicted_route": "engineering",
        }],
    }


def _infra_return(state: AgentState, fix_instruction: str, checkov: dict,
                  validate_passed: bool, plan_passed: bool, raw_error: str = "") -> dict:
    # Chỉ bump total_val_attempts — không increment per-agent counter (tránh nhiễu error_history).
    state["total_val_attempts"] += 1
    new_total = state["total_val_attempts"]
    logger.info("Agent 4: INFRASTRUCTURE — %s", fix_instruction[:80])
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "INFRASTRUCTURE", "root_cause": None,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "INFRASTRUCTURE", "root_cause": None,
            "fix_instruction": fix_instruction, "predicted_route": "requires_human",
        }],
    }


def _fail_return(state: AgentState, error_type: str, root_cause: str,
                 fix_instruction: str, checkov: dict, validate_passed: bool,
                 plan_passed: bool, raw_error: str = "") -> dict:
    """Tạo fix_feedback cho SYNTAX, LOGIC, MISSING_RESOURCE errors.

    Là hàm chung cho cả 3 loại lỗi "fixable":
      - SYNTAX: code không parse được → A3 sửa syntax
      - LOGIC: plan logic sai (reference hỏng, giá trị sai) → A3 sửa logic
      - MISSING_RESOURCE: resource type không tồn tại → A1 re-plan

    Tracking: increment "val_eng" cho SYNTAX/LOGIC, "val_arch" cho MISSING_RESOURCE.
    routing_log: append entry để audit trail.
    """
    assert error_type in ("SYNTAX", "LOGIC", "MISSING_RESOURCE"), \
        f"_fail_return: unexpected error_type '{error_type}'"
    new_total = state["total_val_attempts"] + 1
    is_eng  = error_type in ("SYNTAX", "LOGIC")
    is_arch = error_type == "MISSING_RESOURCE"

    if is_eng:
        increment_retry(state, "val_eng", error_type, raw_error[:200])
    elif is_arch:
        increment_retry(state, "val_arch", error_type, raw_error[:200])

    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "predicted_route": root_cause,
        }],
    }


def _llm_classify(context: str, allowed_types: set,
                  default_type: str, default_fix: str) -> tuple[str, str, str]:
    """LLM phân loại error type và sinh fix_instruction từ terraform error context.

    Hybrid approach: pattern matching trước (tất định, không tốn LLM), LLM sau (cho
    những lỗi ambiguous mà pattern không bắt được).

    allowed_types: tập error type LLM được phép trả.
      - Terraform validate → {"SYNTAX"} (validate chỉ bắt syntax)
      - Terraform plan → {"LOGIC", "MISSING_RESOURCE"} (plan có thể cả hai)
    Nếu LLM trả type ngoài allowed → dùng default_type (safe fallback).

    Fallback: nếu LLM call fail (timeout, parse error) → (default_type, root, default_fix).
    Không raise exception để không làm sập pipeline vì LLM classify error.

    Returns: (error_type, root_cause, fix_instruction)
    """
    def _root(et: str) -> str:
        # MISSING_RESOURCE → A1 (architecture); SYNTAX/LOGIC → A3 (engineering)
        return {"MISSING_RESOURCE": "architecture"}.get(et, "engineering")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    try:
        raw = call_llm(messages, agent="validation")
        parsed = parse_llm_json(raw, {"error_type": None, "fix_instruction": None})
    except Exception as e:
        logger.warning("Agent 4 LLM classify lỗi (%s) — dùng default", e)
        return default_type, _root(default_type), default_fix

    et = parsed.get("error_type")
    if et not in allowed_types:
        et = default_type
    fix = str(parsed.get("fix_instruction") or default_fix)[:1500]
    return et, _root(et), fix


def _extract_failing_resource_body(plan_err: str, code: str) -> str:
    """Trích HCL body của resource(s) xuất hiện trong plan error.

    LLM classify PLAN lỗi không thấy code hiện tại — chỉ thấy error text + label list.
    Cung cấp full block giúp LLM biết resource đang có gì, cần thêm/sửa attribute nào.
    Giới hạn 2 resource và 600 chars để tránh token bloat.
    """
    error_labels = [f"{t}.{n}" for t, n in _RESOURCE_DECL_RE.findall(plan_err)]
    if not error_labels:
        return ""
    blocks: list[str] = []
    seen: set[str] = set()
    for label in error_labels:
        if label in seen or len(blocks) >= 2:
            break
        seen.add(label)
        dot = label.find(".")
        if dot < 0:
            continue
        rtype, rname = label[:dot], label[dot + 1:]
        opener = re.compile(
            rf'resource\s+"({re.escape(rtype)})"\s+"({re.escape(rname)})"\s*\{{',
            re.MULTILINE,
        )
        m = opener.search(code)
        if not m:
            continue
        start, depth = m.start(), 0
        for i, ch in enumerate(code[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(code[start : i + 1])
                    break
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    if len(body) > 600:
        body = body[:600] + "\n  ... (truncated)"
    return f"CURRENT HCL OF AFFECTED RESOURCE(S):\n{body}\n\n"


def _format_prev_fixes(state: AgentState) -> str:
    """Render fix_instruction text từ eng_error_history để bơm vào LLM classify context.

    Tại sao cần?
    error_history trong RetryTracker chỉ lưu error_type string (["SYNTAX","SYNTAX"]) —
    đủ để LLM biết có bao nhiêu lần fail và loại gì, nhưng KHÔNG đủ để LLM biết nội
    dung fix nào đã được thử và vẫn sai. Hệ quả: LLM có thể generate lại đúng fix cũ
    (ROW 7 round 4 lặp lại password-length fix của round 1).

    eng_error_history lưu fix_instruction text đầy đủ — build section này để LLM tránh
    tái tạo fix đã thất bại và tập trung vào lỗi còn lại.

    Returns empty string nếu không có lịch sử (lần đầu A4 chạy).
    """
    hist = (state.get("eng_error_history") or [])[-2:]
    if not hist:
        return ""
    lines = ["PREVIOUSLY ATTEMPTED FIXES (already tried — do NOT repeat these):"]
    for i, e in enumerate(hist, 1):
        fix = (e.get("fix_instruction") or "")[:300].strip()
        if fix:
            lines.append(f"  {i}. {fix}")
    return "\n".join(lines) + "\n"


def validation_node(state: AgentState) -> dict:
    """LangGraph node — validate + plan. Security grading độc lập ở score.py."""
    code = state["generated_code"]
    plan_timeout = state.get("terraform_plan_timeout", 120)
    # Checkov result rỗng dùng khi không chạy đến Checkov stage
    _no_checkov = {"passed_count": 0, "failed": []}

    # Guard: generated_code rỗng = A3 fail tạo code. Route về A1 re-plan
    # (vì nếu code rỗng, khả năng là A1 plan sai → A3 không có gì để serialize).
    if not (code or "").strip():
        return _fail_return(
            state, "MISSING_RESOURCE", "architecture",
            "generated_code rỗng — Engineering agent không sinh được HCL.",
            _no_checkov, False, False,
        )

    run_dir = state.get("run_dir") or ""
    # files_dir: thư mục chứa stub files (Lambda zip, etc.) để write_terraform_dir copy vào
    files_dir = (Path(run_dir) / "files") if run_dir else None

    # reuse=True: giữ .terraform/ từ iteration trước (A4 retry trong cùng run_dir).
    # A3 không thay đổi required_providers → lock file/provider binary vẫn valid.
    with terraform_workdir(run_dir or None, "a4", reuse=bool(run_dir)) as d:
        # Ghi HCL + stubs vào working directory (main.tf + stub files cho Lambda/S3)
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── terraform init ─────────────────────────────────────────────────────
        # Skip nếu marker tồn tại = init thành công ở iteration trước.
        # Marker chỉ được ghi sau init thành công (không ghi nếu fail) → partial
        # .terraform/ từ failed init sẽ KHÔNG bị reuse.
        _marker = Path(d) / _INIT_MARKER
        if _marker.exists():
            logger.info("Agent 4: reusing previous init — skip terraform init")
        else:
            try:
                init = run_terraform(tf_init_cmd(), d, _INIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                return _infra_return(state, f"terraform init timed out (>{_INIT_TIMEOUT}s)", _no_checkov, False, False)
            if init.returncode != 0:
                init_err = ((init.stderr or "") + "\n" + (init.stdout or "")).strip()
                # "problems with the configuration" / "Error: Invalid" = code syntax issue
                if "problems with the configuration" in init_err or init_err.startswith("Error: Invalid"):
                    return _fail_return(
                        state, "SYNTAX", "engineering",
                        INIT_FIX.format(err=init_err[:600]),
                        _no_checkov, False, False, raw_error=init_err[:2000],
                    )
                # Còn lại: provider download fail, network issue, plugin cache issue → INFRA
                return _infra_return(state, f"terraform init failed: {init_err[:500]}", _no_checkov, False, False, raw_error=init_err[:2000])
            # Init thành công — ghi marker để iteration sau skip re-init
            _marker.write_text("")

        # ── terraform validate ─────────────────────────────────────────────────
        # Static check: không cần network, không gọi AWS API.
        # Phát hiện: typo attribute name, missing required argument, wrong type, etc.
        # Luôn là SYNTAX error (validate không biết resource có tồn tại hay không).
        try:
            val = run_terraform(["terraform", "validate", "-no-color"], d, _VALIDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, "terraform validate timed out", _no_checkov, False, False)

        if val.returncode != 0:
            validate_err = (val.stderr or val.stdout or "").strip()
            logger.info("Agent 4: FAIL SYNTAX (validate)")
            # Không cần LLM classify cho SYNTAX — validate error đã có line number +
            # attribute name đủ để A3 fix trực tiếp. Tiết kiệm ~3-5s per SYNTAX retry.
            code_ctx = _extract_code_context(validate_err, code, window=8, max_errors=4)
            fix = "terraform validate failed — fix ALL errors in ONE revision:\n" + validate_err[:1500]
            if code_ctx:
                fix += "\n\nFAILING CODE (>>> = error line):\n" + code_ctx
            return _fail_return(
                state, "SYNTAX", "engineering", fix,
                _no_checkov, False, False, raw_error=validate_err[:2000])

        # ── terraform plan ─────────────────────────────────────────────────────
        # -out=tfplan.out: lưu binary plan để terraform show -json bên dưới.
        # Tại sao lưu plan? Checkov scan plan JSON chính xác hơn scan source:
        #   resolved computed values, for_each expansion, graph checks đầy đủ.
        plan_passed, plan_err = True, ""
        for attempt in range(_MAX_PLAN_TRANSIENT_RETRY + 1):
            try:
                plan = run_terraform(
                    ["terraform", "plan", "-no-color", "-out=tfplan.out"], d, plan_timeout)
            except subprocess.TimeoutExpired:
                return _infra_return(state, f"terraform plan timed out (>{plan_timeout}s)", _no_checkov, True, False)
            plan_passed = plan.returncode == 0
            plan_err = (plan.stderr or plan.stdout or "").strip()
            if plan_passed or not matches_any(plan_err, _PLAN_TRANSIENT_PATTERNS):
                break
            if attempt < _MAX_PLAN_TRANSIENT_RETRY:
                logger.info("Agent 4: plan transient (attempt %d) — retry: %s",
                            attempt + 1, plan_err[:120])
                time.sleep(_PLAN_RETRY_BACKOFF * (attempt + 1))

        # ── terraform show -json (bên trong workdir, trước khi context exit) ──
        # Phải chạy trước khi ra khỏi `with terraform_workdir` vì tfplan.out nằm trong d.
        plan_json_str: str | None = None
        if plan_passed:
            try:
                show = run_terraform(["terraform", "show", "-json", "tfplan.out"], d, _SHOW_TIMEOUT)
                if show.returncode == 0 and show.stdout:
                    plan_json_str = show.stdout
            except Exception as e:
                logger.warning("Agent 4: terraform show -json lỗi (%s) — sẽ fallback source scan", e)

    # ── Post-plan processing ───────────────────────────────────────────────────
    # (ra khỏi terraform_workdir context manager — workdir đã được cleanup)
    if plan_passed:
        # ── Security gate: Checkov targeted scan ─────────────────────────────
        # Chỉ chạy sau khi plan PASS (code hợp lệ về mặt Terraform).
        profile = state.get("security_profile") or {}
        target_ids, per_res = _targets_for_plan(profile)
        if not target_ids:
            # Phân biệt "A2 chủ động chọn 0 check" vs "A2 hỏng (degraded)".
            # Cả hai đều không block (triết lý working-IaC), nhưng degraded phải
            # được đánh dấu — nếu không, một lần LLM A2 timeout sẽ thành PASS thầm
            # với 0 security enforcement.
            degraded = state.get("security_status") == "degraded"
            if degraded:
                logger.warning("Agent 4: security gate SKIPPED — A2 degraded (LLM fail), KHÔNG phải intent")
            else:
                logger.info("Agent 4: PASS (no security target)")
            result = _success_result(_no_checkov, security_degraded=degraded)
            if degraded:
                result["routing_log"] = state["routing_log"] + [{
                    "round": state["total_val_attempts"],
                    "error_type": None, "root_cause": None,
                    "fix_instruction": "security gate bypassed — A2 degraded",
                    "predicted_route": "deployment",
                }]
            return result

        try:
            if plan_json_str:
                # Ưu tiên: scan plan JSON — chính xác hơn source scan.
                checkov = run_checkov_on_plan(plan_json_str, check_ids=sorted(target_ids))
                # Fallback nếu plan framework trả rỗng (check không support plan scan)
                if checkov["total_checks"] == 0:
                    logger.info("Agent 4: plan scan trả 0 checks — fallback source scan")
                    checkov = run_checkov_on_hcl(code, check_ids=sorted(target_ids))
            else:
                checkov = run_checkov_on_hcl(code, check_ids=sorted(target_ids))
        except Exception as e:
            logger.warning("Agent 4: Checkov scan lỗi (%s) — bỏ qua security gate", e)
            return _success_result(_no_checkov)

        # Tìm unmet: checks fail VÀ nằm trong target của resource đó
        unmet = _enforceable_unmet(per_res, checkov)
        # Phantom: target nhưng Checkov không evaluate (resource không trigger check).
        # Ví dụ: CKV_AWS_70 (S3 bucket policy) — nếu không có aws_s3_bucket_policy companion
        # thì Checkov SKIP (không pass, không fail) → phantom enforcement.
        evaluated = set(checkov.get("passed_ckv_ids", [])) | set(checkov.get("failed_ckv_ids", []))
        phantom = sorted(target_ids - evaluated)

        if unmet:
            # Kiểm tra budget trước khi retry
            can_retry, reason = check_retry_budget(state, "sec", max_retries=MAX_VAL_SEC_RETRY)
            if can_retry:
                # Còn budget → route A3 để fix security (hết retry → fall through bên dưới)
                return _security_return(state, unmet, checkov, phantom)
            else:
                logger.info("Agent 4: PASS (best-effort) — %s; phantom=%d", reason, len(phantom))

        # Reach here: unmet=[] (pass clean) HOẶC unmet có nhưng hết budget (best-effort)
        if unmet:
            logger.info("Agent 4: PASS (best-effort) — %d unmet sau %d retry; phantom=%d",
                        len(unmet), MAX_VAL_SEC_RETRY, len(phantom))
        else:
            logger.info("Agent 4: PASS — security enforced ok; phantom=%d", len(phantom))
        # Trả success với unmet (nếu có) → evaluate.py ghi vào val_result["unmet_checks"]
        return _success_result(checkov, unmet, phantom)

    # ── Plan fail handling ─────────────────────────────────────────────────────
    # Infrastructure patterns: network/auth/throttle → không thể fix ở code level
    if matches_any(plan_err, _PLAN_INFRA_PATTERNS):
        return _infra_return(state, f"terraform plan failed (infra): {plan_err[:300]}",
                             _no_checkov, True, False, raw_error=plan_err[:2000])

    # MISSING_RESOURCE: pattern-based detection (tất định, không cần LLM)
    # "not found"/"does not exist" = resource type không tồn tại trong AWS provider
    # → A1 cần re-plan với resource type đúng
    if matches_any(plan_err, MISSING_RESOURCE_PATTERNS):
        error_type, root_cause = "MISSING_RESOURCE", "architecture"
        fix_instruction = f"terraform plan: resource not found or unsupported: {plan_err[:300]}"
        logger.info("Agent 4: FAIL MISSING_RESOURCE (plan pattern)")
        return _fail_return(state, error_type, root_cause, fix_instruction,
                            _no_checkov, True, False, raw_error=plan_err[:2000])

    # LLM classify: lỗi plan không khớp pattern nào → LLM phán LOGIC hay MISSING_RESOURCE
    # Cung cấp: full error text + resource labels + lịch sử lỗi (tránh lặp lại sai lầm)
    eng_history = (state.get("retries") or {}).get("val_eng", {}).get("error_history", [])
    prev_fixes_str = _format_prev_fixes(state)
    failing_body = _extract_failing_resource_body(plan_err, code)
    ctx = _TOP + PLAN_CONTEXT.format(
        plan_err=plan_err[:1500],
        labels=_hcl_resource_labels(code),
        failing_resource_body=failing_body,
        history=json.dumps(eng_history[-3:]),
        prev_fixes=prev_fixes_str,
    ) + _BOTTOM
    error_type, root_cause, fix_instruction = _llm_classify(
        ctx, {"LOGIC", "MISSING_RESOURCE"}, "LOGIC",
        f"terraform plan failed: {plan_err[:300]}")
    logger.info("Agent 4: FAIL %s (plan)", error_type)

    return _fail_return(state, error_type, root_cause, fix_instruction,
                        _no_checkov, True, False, raw_error=plan_err[:2000])


def route_after_validation(state: AgentState) -> str:
    """Conditional edge sau A4 — quyết định node tiếp theo.

    Thứ tự kiểm tra:
      1. overall_passed            → deployment
      2. total_val_attempts >= 5       → requires_human  (validation-phase backstop; deploy
                                      phase dùng total_deploy_attempts riêng ở route_after_deployment)
      3. INFRASTRUCTURE            → requires_human
      4. root_cause invalid        → requires_human
      5. Budget check (trừ SECURITY — đã check in-node) → requires_human nếu cạn
      6. Route: root_cause "engineering" → A3 | "architecture" → A1
    """
    # ── Pass: route deployment ─────────────────────────────────────────────────
    if state["fix_feedback"]["overall_passed"]:
        return "deployment"

    error_type = state["fix_feedback"]["error_type"]

    if state["total_val_attempts"] >= MAX_TOTAL_RETRY:
        logger.info("Route: max total attempts (%d >= %d)", state["total_val_attempts"], MAX_TOTAL_RETRY)
        return "requires_human"

    if error_type == "INFRASTRUCTURE":
        logger.info("Route: INFRASTRUCTURE — requires_human")
        return "requires_human"

    root_cause = state["fix_feedback"].get("root_cause")
    _AGENT = {"engineering": "val_eng", "architecture": "val_arch"}
    agent = _AGENT.get(root_cause)
    if agent is None:
        logger.error("Route: invalid root_cause '%s' — requires_human", root_cause)
        return "requires_human"

    # SECURITY: budget check xảy ra in-node (validation_node dùng sec counter + best-effort logic).
    # SYNTAX/LOGIC/MISSING: budget check ở đây sau khi node đã return.
    # Lý do tách tầng: SECURITY exhaustion không block deploy (best-effort), còn SYNTAX/LOGIC thì có.
    if error_type != "SECURITY":
        _MAX = {"val_eng": MAX_VAL_ENG_RETRY, "val_arch": MAX_VAL_ARCH_RETRY}
        can_retry, reason = check_retry_budget(state, agent, max_retries=_MAX[agent])
        if not can_retry:
            logger.info("Route: %s — requires_human", reason)
            return "requires_human"

    return root_cause  # "engineering" → A3 | "architecture" → A1
