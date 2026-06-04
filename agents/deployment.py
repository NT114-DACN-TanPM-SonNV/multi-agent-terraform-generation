"""Agent 5 — Deployment: terraform apply lên AWS.

Fail → cleanup partial state (destroy) → phân loại → route.
INFRASTRUCTURE: in-node retry 1 lần. LOGIC → A3. MISSING_RESOURCE → A1.
OTHER/dirty state → requires_human. Auto-destroy sau apply trong eval mode.
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

# Patch HCL trước khi destroy trong eval mode — tắt các attribute chặn delete API.
# Thứ tự quan trọng: final_snapshot_identifier phải xử lý sau skip_final_snapshot.
_DESTROY_PATCHES = [
    (r'(deletion_protection_enabled\s*=\s*)true',    r'\g<1>false'),  # DynamoDB
    (r'(deletion_protection\s*=\s*)true',            r'\g<1>false'),  # RDS/ALB
    (r'(skip_final_snapshot\s*=\s*)false',           r'\g<1>true'),   # RDS
    (r'\n[ \t]*final_snapshot_identifier\s*=\s*[^\n]+', ''),          # RDS (conflicts với skip)
    (r'(apply_immediately\s*=\s*)false',             r'\g<1>true'),   # RDS
    (r'(automatic_failover_enabled\s*=\s*)true',     r'\g<1>false'),  # ElastiCache
    (r'(multi_az_enabled\s*=\s*)true',               r'\g<1>false'),  # ElastiCache
]


def _patch_for_destroy(code: str) -> str:
    """Chỉ dùng trong eval mode (auto_destroy=True)."""
    for pattern, replacement in _DESTROY_PATCHES:
        code = re.sub(pattern, replacement, code)
    return code


from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir, tf_init_cmd
from core.retry_control import (
    increment_retry, check_retry_budget,
    MAX_DEPLOY_TOTAL_RETRY, MAX_DEPLOY_ENG_RETRY, MAX_DEPLOY_ARCH_RETRY,
)
from core.errors import matches_any, MISSING_RESOURCE_PATTERNS, AUTH_PATTERNS
from prompts.deployment import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.deployment import (
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM, CLASSIFY_CONTEXT,
)

logger = logging.getLogger(__name__)

_INIT_TIMEOUT    = 60
_APPLY_TIMEOUT   = 360
_DESTROY_TIMEOUT = 600   # ElastiCache/RDS cần 5-10 phút để xóa
_STATE_TIMEOUT = 30

# In-node retry cho lỗi INFRASTRUCTURE (transient: network/throttle/quota tạm thời).
# Đối xứng A4 (_MAX_PLAN_TRANSIENT_RETRY): retry 1 lần, có backoff trước khi thử lại.
# Backoff để KHÔNG hammer API đang throttle (throttling/rate exceeded nằm trong
# _INFRASTRUCTURE_PATTERNS) — A4 đã làm vậy, A5 trước đây retry tức thì (thiếu nhịp chờ).
_MAX_APPLY_TRANSIENT_RETRY = 1
_APPLY_RETRY_BACKOFF = 5  # giây chờ trước khi retry in-node

# Dùng cụm cụ thể (không bare "timeout"/"eof") vì apply output echo cả config HCL
# có thể chứa `timeouts {}` hay heredoc "EOF" → false positive nếu dùng bare keyword.
_INFRASTRUCTURE_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "i/o timeout", "timed out", "context deadline exceeded",
    "tls handshake timeout", "no such host", "dial tcp",
    "reset by peer", "unexpected eof", "requesttimeout",
    "requestlimitexceeded", "throttling", "rate exceeded",
    "vpcquotaexceeded", "limitexceeded",
)

# Credential/permission errors → OTHER (terminal, no retry). "InsufficientInstanceCapacity"
# không vào đây — đó là capacity transient, không phải permission.
_PERMISSION_PATTERNS = AUTH_PATTERNS   # sync với A4 qua core.errors



def _extract_error(stdout: str, stderr: str) -> str:
    """stderr đầy đủ + tail của stdout (stderr ngắn bị cắt nếu chỉ lấy tail combined).
    Thêm section "Error lines" để LLM focus.
    """
    stderr_clean = (stderr or "").strip()
    stdout_tail = (stdout or "")[-2000:]
    combined = (stderr_clean + "\n" + stdout_tail).strip()
    error_lines = [ln for ln in combined.splitlines() if re.match(r"\s*(?:Error|error):", ln)]
    if error_lines:
        return combined + "\n\n--- Error lines ---\n" + "\n".join(error_lines[-20:])
    return combined


def _resource_labels(plan: dict) -> list[str]:
    """Tạo list "type.name" từ infrastructure_plan — hint cho LLM classify."""
    return [f"{r['type']}.{r['name']}" for r in plan.get("resources", [])]


def _guess_failed_resource(error_text: str, labels: list[str]) -> str | None:
    """Match resource trong error text theo 3 tầng: full label → type → name (word-boundary,
    len>3 để tránh "main"/"this" match bừa).
    """
    for label in labels:
        if label in error_text:
            return label
    for label in labels:
        if label.split(".", 1)[0] in error_text:
            return label
    for label in labels:
        rname = label.split(".", 1)[1]
        if len(rname) > 3 and re.search(rf"\b{re.escape(rname)}\b", error_text):
            return label
    return None


def _deploy_result(success: bool, error_type: str | None, *, fix_instruction=None,
                   resources_created=None, partial_apply_destroyed=False,
                   destroy_failed=False, destroy_error=None, apply_raw_error=None) -> dict:
    # destroy_failed → dirty state → route_after_deployment force requires_human.
    return {
        "success": success,
        "error_type": error_type,
        "resources_created": resources_created or [],
        "partial_apply_destroyed": partial_apply_destroyed,
        "destroy_failed": destroy_failed,
        "destroy_error": destroy_error,
        "fix_instruction": fix_instruction,
        "apply_raw_error": apply_raw_error,
    }


def _state_resources(tmpdir: str) -> list:
    """List resources trong terraform state — dùng để biết partial apply đã tạo gì."""
    try:
        r = run_terraform(["terraform", "state", "list"], tmpdir, _STATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _llm_classify_deploy(
    error_text: str,
    resource_labels: list[str],
    failed_resource: str | None,
    partial: bool,
    destroyed: bool,
    retry: int,
) -> tuple[str, str | None]:
    """Phân loại apply error khi pattern matching không xác định được type.
    Fallback về "OTHER" (terminal) nếu LLM fail — tránh loop LOGIC → A3 sai.

    Tại sao OTHER thay vì LOGIC?
      LOGIC route A3 có thể loop vô hạn nếu LLM classify sai liên tục.
      OTHER route requires_human — conservative hơn, đảm bảo người can thiệp.
    """
    ctx = _TOP + CLASSIFY_CONTEXT.format(
        labels=json.dumps(resource_labels),
        failed=failed_resource or 'unknown',
        error=error_text[:2000],
        partial=partial, destroyed=destroyed, retry=retry,
    ) + _BOTTOM
    try:
        parsed = parse_llm_json(
            call_llm([{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": ctx}], agent="deployment"),
            {"error_type": None, "fix_instruction": None},
        )
    except Exception as e:
        logger.warning("Agent 5 LLM classify error (%s) — OTHER", e)
        return "OTHER", None
    et = parsed.get("error_type")
    if et not in ("LOGIC", "MISSING_RESOURCE", "OTHER"):
        et = "OTHER"
    # fix_instruction chỉ có nghĩa với LOGIC/MISSING (cần A3/A1 fix code)
    # OTHER → requires_human → fix_instruction không được dùng
    fix = parsed.get("fix_instruction") if et in ("LOGIC", "MISSING_RESOURCE") else None
    return et, (str(fix)[:500] if fix else None)


def _route_back_fix_feedback(error_type: str, root_cause: str, fix: str | None) -> dict:
    """fix_feedback chuẩn khi A5 route ngược: LOGIC→A3 (engineering), MISSING→A1 (architecture).

    validate_passed/plan_passed=True vì A4 đã pass — lỗi chỉ xảy ra ở apply-time.
    Gộp 2 block LOGIC/MISSING vốn giống hệt nhau (chỉ khác error_type + root_cause).
    """
    return {
        "overall_passed": False,
        "error_type": error_type,
        "root_cause": root_cause,
        "fix_instruction": fix,
        "checkov": {"passed_count": 0, "failed": []},
        "validate_passed": True,
        "plan_passed": True,
    }


def _routing_log_append(state: AgentState, error_type: str | None,
                        root_cause: str | None, fix: str | None,
                        predicted_route: str) -> list:
    """Thêm 1 entry audit vào routing_log (đối xứng A4 — routing_log là audit chung).

    Trước đây A5 KHÔNG ghi routing_log → audit trail mù toàn bộ vòng deploy.
    `round` = total_val_attempts + total_deploy_attempts (round toàn cục đơn điệu xuyên 2 pha;
    deploy fail bump total_deploy_attempts nên dùng tổng để round vẫn tăng đều ở pha deploy).
    """
    return state["routing_log"] + [{
        "round": state["total_val_attempts"] + state["total_deploy_attempts"],
        "error_type": error_type,
        "root_cause": root_cause,
        "fix_instruction": fix,
        "predicted_route": predicted_route,
    }]


def _handle_failure(
    state: AgentState, tmpdir: str,
    apply_stdout: str, apply_stderr: str,
    is_timeout: bool,
) -> dict:
    """Xử lý apply fail: phân loại lỗi, cleanup partial state, trả dict state update.

    Flow chi tiết:
      1. Pattern-based classification (tất định):
           is_timeout → INFRASTRUCTURE
           _INFRASTRUCTURE_PATTERNS match → INFRASTRUCTURE
           MISSING_RESOURCE_PATTERNS match → MISSING_RESOURCE
           else → None (cần LLM ở bước 3)
      2. Nếu timeout: terraform refresh (rebuild state từ AWS vì state có thể corrupt)
      3. Cleanup: terraform state list → terraform destroy (luôn chạy, no-op nếu state rỗng)
      4. LLM classify (chỉ nếu error_type=None từ bước 1)
      5. Increment retry counter và build result dict
      6. Special handling cho LOGIC và MISSING_RESOURCE (thêm fix_feedback)
    """
    error_text = _extract_error(apply_stdout, apply_stderr)
    plan = state.get("infrastructure_plan") or {}
    resource_labels = _resource_labels(plan)

    # ── Step 1: Pattern-based classification (không cần LLM) ─────────────────
    # Ưu tiên tất định trước LLM: nhanh hơn, không tốn quota, deterministic.
    if is_timeout:
        # apply bị SIGKILL sau _APPLY_TIMEOUT → terraform state có thể bị corrupt
        error_type = "INFRASTRUCTURE"
    elif matches_any(error_text, _PERMISSION_PATTERNS):
        # Quyền/credential thiếu → không fix bằng code, không retry apply → OTHER (human).
        # Đặt TRƯỚC _INFRASTRUCTURE_PATTERNS để KHÔNG bị in-node retry phí 1 lần apply.
        error_type = "OTHER"
    elif matches_any(error_text, _INFRASTRUCTURE_PATTERNS):
        # Network/throttle/quota tạm thời → không phải code bug → in-node retry 1 lần
        error_type = "INFRASTRUCTURE"
    elif matches_any(error_text, MISSING_RESOURCE_PATTERNS):
        # Resource type không tồn tại/không hỗ trợ → A1 cần re-plan
        error_type = "MISSING_RESOURCE"
    else:
        error_type = None  # không xác định được → cần LLM ở bước sau

    # ── Step 2: Refresh state nếu timeout ────────────────────────────────────
    # Khi terraform apply bị SIGKILL giữa chừng, state file có thể rỗng (terraform
    # chưa kịp commit partial state) hoặc stale (chứa resource đã bị roll back).
    # `terraform refresh` query AWS thực tế → rebuild state → destroy sau đó chính xác.
    if is_timeout:
        try:
            run_terraform(["terraform", "refresh", "-no-color"], tmpdir, 60)
        except subprocess.TimeoutExpired:
            pass  # best-effort: nếu refresh cũng timeout thì destroy vẫn chạy

    # ── Step 3: Cleanup partial state ────────────────────────────────────────
    # LUÔN chạy destroy dù apply fail theo cách nào.
    # Lý do: partial apply có thể tạo một số resource (VPC xong, RDS chưa xong).
    # Nếu không cleanup: resource sót lại → leak AWS cost + conflict lần sau.
    # `terraform state list` trước để biết có resource không (partial=True nếu có).
    created = _state_resources(tmpdir)
    partial = bool(created)
    partial_destroyed = destroy_failed = False
    destroy_error = None

    try:
        destroy = run_terraform(
            ["terraform", "destroy", "-auto-approve", "-no-color"],
            tmpdir, _DESTROY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # Destroy timeout → dirty state → người phải can thiệp
        destroy_failed = True
        destroy_error = f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
    else:
        if destroy.returncode == 0:
            partial_destroyed = True  # cleanup thành công
        else:
            destroy_failed = True
            destroy_error = (destroy.stderr or "")[:500]

    # ── Step 4: LLM classify (chỉ khi pattern không xác định được) ──────────
    fix = None
    if error_type is None:
        failed_resource = _guess_failed_resource(error_text, resource_labels)
        error_type, fix = _llm_classify_deploy(
            error_text, resource_labels, failed_resource,
            partial, partial_destroyed, state["retries"]["deploy_eng"]["count"],
        )

    # ── Step 5: Increment retry counter + build base result ──────────────────
    # LOGIC/MISSING_RESOURCE bump total_deploy_attempts qua increment_retry ở Step 6 — TRỪ khi
    # destroy_failed (Step 6 bị skip vì điều kiện `not destroy_failed`) → bump tại đây
    # để total_deploy_attempts không đếm thiếu (giữ deploy-phase backstop + audit chính xác).
    if error_type not in ("LOGIC", "MISSING_RESOURCE") or destroy_failed:
        state["total_deploy_attempts"] += 1

    logger.info(
        "Agent 5: FAIL %s (partial=%s destroyed=%s destroy_failed=%s)",
        error_type, partial, partial_destroyed, destroy_failed,
    )

    # Base result dict — LOGIC/MISSING sẽ thêm fix_feedback vào bên dưới.
    # retries và total_val_attempts: trả về state hiện tại (đã mutate bởi increment_retry).
    result: dict = {
        "deployment_result": _deploy_result(
            False, error_type,
            fix_instruction=fix,
            resources_created=created,
            partial_apply_destroyed=partial_destroyed,
            destroy_failed=destroy_failed,
            destroy_error=destroy_error,
            apply_raw_error=error_text[:3000],
        ),
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "total_deploy_attempts": state["total_deploy_attempts"],
    }

    # ── Step 6: Special handling cho actionable errors ───────────────────────

    # LOGIC: HCL code logic sai (wrong arg, invalid value, circular dependency).
    # A3 có thể fix bằng cách patch specific attribute → route engineering.
    # Điều kiện: destroy phải thành công (nếu destroy fail → dirty state → requires_human).
    if error_type == "LOGIC" and not destroy_failed:
        increment_retry(state, "deploy_eng", "LOGIC_DEPLOY", error_text[:200])
        # fix_feedback với root_cause="engineering" → route_after_deployment → A3
        result["fix_feedback"] = _route_back_fix_feedback("LOGIC", "engineering", fix)
        # Cập nhật retries sau khi increment "deploy_eng" (total_deploy_attempts cũng tăng)
        result["retries"] = state["retries"]
        result["total_val_attempts"] = state["total_val_attempts"]
        result["total_deploy_attempts"] = state["total_deploy_attempts"]

    # MISSING_RESOURCE: resource type không tồn tại → A1 cần re-plan.
    # Ví dụ: A1 plan dùng aws_lambda_event_source_mapping nhưng thiếu aws_sqs_queue.
    elif error_type == "MISSING_RESOURCE" and not destroy_failed:
        increment_retry(state, "deploy_arch", "MISSING_RESOURCE_DEPLOY", error_text[:200])
        result["fix_feedback"] = _route_back_fix_feedback("MISSING_RESOURCE", "architecture", fix)
        result["retries"] = state["retries"]
        result["total_val_attempts"] = state["total_val_attempts"]
        result["total_deploy_attempts"] = state["total_deploy_attempts"]

    # ── Audit: ghi routing_log (đối xứng A4) ─────────────────────────────────
    # predicted_route phản ánh quyết định ở node (chưa tính budget — giống A4):
    #   destroy_failed → human (dirty state) | LOGIC → A3 | MISSING → A1 | còn lại → human
    if destroy_failed:
        predicted_route = "requires_human"
    else:
        predicted_route = {
            "LOGIC": "engineering",
            "MISSING_RESOURCE": "architecture",
        }.get(error_type, "requires_human")
    result["routing_log"] = _routing_log_append(
        state, error_type, result.get("fix_feedback", {}).get("root_cause"),
        fix, predicted_route,
    )

    return result


def deployment_node(state: AgentState) -> dict:
    """LangGraph node — thực thi terraform apply lên AWS.

    Flow:
      1. terraform init: tải provider, setup backend.
         Nếu fail → INFRASTRUCTURE error (init failure không fixable bằng code).
      2. terraform apply: tạo resources trên AWS.
         Nếu success → optional auto-destroy (eval mode).
         Nếu fail → _handle_failure: cleanup + classify + route.

    Tại sao init lại sau A4 đã init?
      A4 và A5 dùng different working directories (terraform_workdir tạo temp dir riêng).
      Provider cache được share qua plugin cache → init lần 2 nhanh hơn (không download lại).
      Nếu run_dir được set: A5 reuse thư mục A4 (reuse=True) → skip init hoàn toàn.

    Chi phí in-node retry: khi apply INFRASTRUCTURE ở attempt=0, _handle_failure chạy
      terraform destroy (cleanup partial state) trước khi retry. Với ElastiCache/RDS,
      destroy mất 5–10 phút → tổng 1 in-node retry ~20 phút. Nếu resource chậm,
      consider tăng _APPLY_TIMEOUT.
    """
    code = state["generated_code"]

    # Log retry count để trace: biết A5 đang ở lần retry thứ mấy
    logger.info(
        "Agent 5: deploy_arch_retry=%d deploy_eng_retry=%d",
        state["retries"].get("deploy_arch", {}).get("count", 0),
        state["retries"].get("deploy_eng", {}).get("count", 0),
    )

    run_dir = state.get("run_dir") or ""
    # files_dir: stub files (Lambda zip, S3 object content) cần copy vào working dir
    files_dir = (Path(run_dir) / "files") if run_dir else None

    # reuse=True khi có run_dir: A5 tái sử dụng thư mục A4 đã init (không xóa .terraform/).
    # reuse=False khi không có run_dir: tempdir mới, phải init từ đầu.
    with terraform_workdir(run_dir or None, "a4", reuse=bool(run_dir)) as d:
        # Ghi HCL + stubs vào temp directory
        write_terraform_dir(d, code, files_dir=files_dir)

        # Skip init nếu .terraform/ đã tồn tại (A4 đã init cùng thư mục).
        # Tiết kiệm 10-30s download provider; chỉ khả dụng khi run_dir được set.
        tf_initialized = (Path(d) / ".terraform").exists()
        if tf_initialized:
            logger.info("Agent 5: reusing A4 init — skip terraform init")

        # ── terraform init + apply — retry in-node nếu INFRASTRUCTURE ──
        # Giống A4: transient issue (network/quota) tự hết sau 1 lần chờ → retry.
        # Không qua graph (tránh overhead routing + state cycle không cần thiết).
        for attempt in range(_MAX_APPLY_TRANSIENT_RETRY + 1):
            if attempt > 0:
                # Backoff trước khi retry in-node — tránh hammer API đang throttle
                # (throttling/rate exceeded ∈ _INFRASTRUCTURE_PATTERNS). Đối xứng A4
                # (_PLAN_RETRY_BACKOFF); trước đây A5 retry tức thì, thiếu nhịp chờ.
                time.sleep(_APPLY_RETRY_BACKOFF * attempt)
            # ── terraform init (chỉ khi chưa có .terraform/) ─────────────────
            if not tf_initialized:
                logger.info("Agent 5: terraform init attempt=%d (timeout=%ds)", attempt, _INIT_TIMEOUT)
                try:
                    init = run_terraform(tf_init_cmd(), d, _INIT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    if attempt == 0:
                        logger.warning("Agent 5: init timeout — retry in-node")
                        continue
                    logger.error("Agent 5: init timeout (sau retry)")
                    state["total_deploy_attempts"] += 1
                    return {
                        "deployment_result": _deploy_result(
                            False, "INFRASTRUCTURE",
                            fix_instruction=f"terraform init timed out (>{_INIT_TIMEOUT}s)",
                        ),
                        "retries": state["retries"],
                        "total_val_attempts": state["total_val_attempts"],
                        "total_deploy_attempts": state["total_deploy_attempts"],
                    }

                if init.returncode != 0:
                    if attempt == 0:
                        logger.warning("Agent 5: init failed — retry in-node")
                        continue
                    logger.error("Agent 5: init FAILED (sau retry)")
                    state["total_deploy_attempts"] += 1
                    return {
                        "deployment_result": _deploy_result(
                            False, "INFRASTRUCTURE",
                            fix_instruction=f"terraform init failed: {init.stderr[:300]}",
                        ),
                        "retries": state["retries"],
                        "total_val_attempts": state["total_val_attempts"],
                        "total_deploy_attempts": state["total_deploy_attempts"],
                    }

            # ── terraform apply ───────────────────────────────────────────────
            logger.info("Agent 5: terraform apply attempt=%d (timeout=%ds)", attempt, _APPLY_TIMEOUT)
            try:
                apply = run_terraform(
                    ["terraform", "apply", "-auto-approve", "-no-color"], d, _APPLY_TIMEOUT
                )
            except subprocess.TimeoutExpired:
                _attempts_before = state["total_deploy_attempts"]
                failure = _handle_failure(state, d, "", "terraform apply timed out", is_timeout=True)
                if attempt == 0 and failure["deployment_result"]["error_type"] == "INFRASTRUCTURE":
                    # In-node retry trong suốt với graph budget (đối xứng A4 plan-transient):
                    # hoàn lại total_deploy_attempts mà _handle_failure đã bump cho attempt sắp retry.
                    state["total_deploy_attempts"] = _attempts_before
                    logger.warning("Agent 5: apply timeout — retry in-node")
                    continue
                return failure

            if apply.returncode != 0:
                _attempts_before = state["total_deploy_attempts"]
                failure = _handle_failure(state, d, apply.stdout or "", apply.stderr or "", is_timeout=False)
                if attempt == 0 and failure["deployment_result"]["error_type"] == "INFRASTRUCTURE":
                    # In-node retry trong suốt với graph budget (đối xứng A4 plan-transient):
                    # hoàn lại total_deploy_attempts mà _handle_failure đã bump cho attempt sắp retry.
                    state["total_deploy_attempts"] = _attempts_before
                    logger.warning("Agent 5: apply INFRASTRUCTURE — retry in-node")
                    continue
                return failure

            break  # apply success

        if apply.returncode == 0:
            # ── Apply success ─────────────────────────────────────────────────
            # Lấy danh sách resource đã tạo từ terraform state (cho deployment_result).
            created = _state_resources(d)
            logger.info("Agent 5: APPLY OK — %d resources", len(created))

            auto_destroyed = False
            auto_destroy_error = None
            if state.get("auto_destroy"):
                # Eval mode: cleanup resources ngay sau apply thành công.
                # Tại sao patch trước? Deletion protection chặn destroy API.
                logger.info("Agent 5: auto-destroy (eval mode)")
                tf_path = Path(d) / "main.tf"
                original = tf_path.read_text(encoding="utf-8")
                patched = _patch_for_destroy(original)
                if patched != original:
                    logger.info("Agent 5: patching deletion-protection attrs before destroy")
                    tf_path.write_text(patched, encoding="utf-8")
                    # Re-apply patched code để AWS nhận thấy thay đổi attribute trước destroy
                    try:
                        run_terraform(
                            ["terraform", "apply", "-auto-approve", "-no-color"],
                            d, _APPLY_TIMEOUT,
                        )
                    except subprocess.TimeoutExpired:
                        pass  # best-effort: thử destroy dù patch re-apply fail
                # Destroy với timeout dài (ElastiCache/RDS cần 5-10 phút)
                try:
                    cleanup = run_terraform(
                        ["terraform", "destroy", "-auto-approve", "-no-color"],
                        d, _DESTROY_TIMEOUT,
                    )
                    auto_destroyed = cleanup.returncode == 0
                    if not auto_destroyed:
                        auto_destroy_error = (cleanup.stderr or "")[:300]
                        logger.warning("Agent 5: auto-destroy FAILED — %s", auto_destroy_error)
                    else:
                        logger.info("Agent 5: auto-destroy OK")
                except subprocess.TimeoutExpired:
                    auto_destroy_error = f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
                    logger.warning("Agent 5: auto-destroy TIMEOUT")

            result = _deploy_result(True, None, resources_created=created)
            result["auto_destroyed"] = auto_destroyed
            result["auto_destroy_error"] = auto_destroy_error
            # Success: chỉ cần deployment_result (không cần fix_feedback, retries đã ổn)
            return {"deployment_result": result}

        # ── Apply fail → cleanup + classify + route ───────────────────────────
        return _handle_failure(
            state, d, apply.stdout or "", apply.stderr or "", is_timeout=False
        )


def route_after_deployment(state: AgentState) -> str:
    """Conditional edge sau A5 — quyết định node tiếp theo.

    Thứ tự kiểm tra (ĐỐI XỨNG với route_after_validation):
      1. Success → end (done)
      2. destroy_failed → requires_human (dirty state, LUÔN cần human)
      3. total_deploy_attempts >= MAX_DEPLOY_TOTAL_RETRY → requires_human (deploy-phase backstop, ĐỘC LẬP A4)
      4. INFRASTRUCTURE → requires_human (đã retry in-node rồi)
      5. LOGIC → route A3 nếu còn budget (code bug, A3 fix)
      6. MISSING_RESOURCE → route A1 nếu còn budget (resource type sai, A1 re-plan)
      7. Mọi trường hợp còn lại (OTHER, PERMISSION, QUOTA, exhausted) → requires_human
    """
    dr = state["deployment_result"]

    # Success: pipeline hoàn thành → kết thúc
    if dr["success"]:
        return "end"

    # Dirty state: resources tồn tại trên AWS nhưng không thể destroy.
    # Không retry bất kỳ gì — người phải cleanup thủ công trước khi chạy lại.
    # Đặt TRƯỚC global cap vì đây là vấn đề an toàn (dirty state) chứ không phải budget.
    if dr.get("destroy_failed"):
        return "requires_human"

    # Deploy-phase backstop — ĐỘC LẬP với total_val_attempts của validation phase.
    # total_deploy_attempts chỉ tăng bởi fail của A5 (increment_retry deploy_* + các nhánh
    # infra/timeout trong node). Tách khỏi total_val_attempts để A4 đốt hết budget của nó
    # KHÔNG starve A5: lỗi apply-time là lớp mới, A5 phải có lượt sửa riêng.
    # Per-counter deploy_eng/deploy_arch (≤2) vẫn là sub-limit của riêng lớp deploy;
    # backstop này chống explosion khi re-plan reset các per-agent counter.
    if state["total_deploy_attempts"] >= MAX_DEPLOY_TOTAL_RETRY:
        logger.info("Agent 5: max deploy attempts (%d >= %d) — requires_human",
                    state["total_deploy_attempts"], MAX_DEPLOY_TOTAL_RETRY)
        return "requires_human"

    error_type = dr["error_type"]

    # INFRASTRUCTURE: đã retry in-node 1 lần rồi → requires_human.
    if error_type == "INFRASTRUCTURE":
        return "requires_human"

    if error_type == "LOGIC":
        can_retry, reason = check_retry_budget(state, "deploy_eng", max_retries=MAX_DEPLOY_ENG_RETRY)
        if can_retry:
            return "engineering"
        logger.info("Agent 5: %s — route requires_human", reason)
        return "requires_human"

    if error_type == "MISSING_RESOURCE":
        can_retry, reason = check_retry_budget(state, "deploy_arch", max_retries=MAX_DEPLOY_ARCH_RETRY)
        if can_retry:
            return "architecture"
        logger.info("Agent 5: %s — route requires_human", reason)
        return "requires_human"

    # OTHER / PERMISSION / QUOTA / UNKNOWN: không có code fix.
    # Ví dụ: IAM permission thiếu, service limit, S3 bucket name conflict.
    # Người phải xem xét và fix AWS setup.
    logger.info("Agent 5: route requires_human (error_type=%s)", error_type)
    return "requires_human"
