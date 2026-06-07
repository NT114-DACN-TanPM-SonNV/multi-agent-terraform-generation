from typing import TypedDict


class RetryTracker(TypedDict):
    """Tracker retry cho một agent — counter + lịch sử lỗi."""
    count: int              # số lần đã retry
    last_error_type: str    # loại lỗi cuối (debug)
    last_error_details: str # mô tả lỗi cuối (debug)
    error_history: list     # list[str] — 5 error_type gần nhất, bơm vào prompt LLM (tránh lặp lỗi cũ) + debug


class AgentState(TypedDict):
    """Shared memory duy nhất chảy qua toàn pipeline.

    LangGraph truyền state qua mọi node. Node đọc các field cần thiết,
    rồi trả dict update — LangGraph merge vào state trước khi gọi node tiếp theo.
    Không ai giữ reference riêng; state là single source of truth.
    """

    # ── Input (bất biến suốt pipeline) ────────────────────────────────────────
    prompt: str  # user request gốc, không bao giờ thay đổi

    # ── Cấu hình chạy ─────────────────────────────────────────────────────────
    terraform_plan_timeout: int  # giây — đọc từ TF_PLAN_TIMEOUT env

    # ── Output từng agent ─────────────────────────────────────────────────────
    # A1: JSON plan mô tả AWS resources cần tạo
    # Schema: {"resources": [{"type", "name", "attributes", "blocks"}],
    #          "data_sources": [...]}
    # attributes = HCL arg = value; blocks = HCL block {} (không có =)
    # "REF:type.name.attr" = reference tới resource khác trong plan
    infrastructure_plan: dict

    # A2: CKV IDs cần enforce, grounded bằng catalog menu per resource
    # Schema: {"aws_s3_bucket.main": {"type": "aws_s3_bucket", "checks": ["CKV_AWS_19"]}}
    # checks=[] nghĩa là A2 không chọn check nào cho resource đó (intent hoặc lỗi)
    security_profile: dict

    # A2 status: "ok" (A2 chạy xong, kể cả khi chủ động chọn 0 check) vs
    # "degraded" (LLM hỏng → profile rỗng KHÔNG do intent). Tách 2 trạng thái này
    # để A4 không PASS security gate thầm và eval/score đếm được run bị degrade.
    security_status: str

    # A3: HCL Terraform hoàn chỉnh (terraform{} + provider{} + resource{} blocks)
    generated_code: str

    # A4: kết quả validate/plan/checkov + routing hint cho node tiếp theo
    # Schema: {"overall_passed": bool, "error_type": str|None,
    #          "root_cause": "engineering"|"architecture"|None,
    #          "fix_instruction": str|None, "error_label": str|None,
    #          "error_stage": str|None, "checkov": {...},
    #          "applicable_failed_checks": [...], "not_applicable_checks": [...],
    #          "security_degraded": bool,
    #          "validate_passed": bool, "plan_passed": bool}
    # A5 ghi đè fix_feedback khi cần route lại A3/A1 sau apply fail.
    fix_feedback: dict

    # A5: kết quả terraform apply
    # Schema: {"success": bool, "error_type": str|None,
    #          "error_label": str|None, "cleanup_error_label": str|None,
    #          "resources_created": [...], "apply_raw_error": str,
    #          "partial_apply_destroyed": bool, "destroy_failed": bool}
    deployment_result: dict

    # ── Retry tracking ─────────────────────────────────────────────────────────
    # A4 và A5 có counter độc lập — lỗi A5 là lớp mới (apply-time), không liên quan A4.
    # val_eng:     A4 → A3 (SYNTAX/LOGIC/SECURITY),  cap = MAX_VAL_ENG_RETRY
    # val_arch:    A4 → A1 (MISSING_RESOURCE),         cap = MAX_VAL_ARCH_RETRY
    # deploy_eng:  A5 → A3 (LOGIC_DEPLOY),             cap = MAX_DEPLOY_ENG_RETRY
    # deploy_arch: A5 → A1 (MISSING_RESOURCE_DEPLOY),  cap = MAX_DEPLOY_ARCH_RETRY
    # sec:         security gate A4, hết → best-effort deploy (không block)
    retries: dict[str, RetryTracker]
    total_val_attempts: int    # validation-phase backstop — max MAX_TOTAL_RETRY=5, tăng mỗi fail của A1/A3/A4
    total_deploy_attempts: int   # deploy-phase backstop — max MAX_DEPLOY_TOTAL_RETRY=4, chỉ A5 fail tăng (độc lập total_val_attempts)

    # ── Oscillation prevention ─────────────────────────────────────────────────
    # Lưu 2 fix_instruction gần nhất gửi tới A1/A3 — đút vào prompt để agent
    # biết "đừng lặp lại sai lầm này". Khác retries[x].error_history: cái đó
    # là chuỗi error_type (để phát hiện pattern), cái này là fix text đầy đủ.
    arch_error_history: list  # list[{"fix_instruction": str}]
    eng_error_history: list   # list[{"fix_instruction": str}]

    # ── Audit ──────────────────────────────────────────────────────────────────
    routing_log: list  # list[{"round", "error_type", "root_cause", "fix_instruction", "predicted_route"}]

    # ── Eval infra ─────────────────────────────────────────────────────────────
    # evaluate.py set trước khi invoke; "" = dùng tempdir tự động.
    # A4 và A5 dùng chung thư mục này để chia sẻ stub files (Lambda zip, etc.)
    # mà không cần copy lại giữa các agent.
    run_dir: str
