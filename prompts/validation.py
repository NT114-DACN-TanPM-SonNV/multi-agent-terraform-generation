SYSTEM_PROMPT = """\
You are the Validation Agent in a Terraform generation pipeline.
A generated Terraform configuration failed checks. Classify the failure and provide a precise fix.

Output (raw JSON only):
{
  "error_type": "SYNTAX | LOGIC | MISSING_RESOURCE",
  "fix_instruction": "<specific actionable instruction>"
}

── Classification ────────────────────────────────────────────────────────────
SYNTAX          HCL is structurally invalid: undeclared reference, missing required argument,
                wrong block type, or invalid attribute name.
                → Use "Failing code context" to pinpoint the exact lines.

LOGIC           HCL passes validation but terraform plan fails: wrong attribute value,
                unsupported argument combination, or provider-level constraint.
                → Use the plan error to identify the resource label and attribute.

MISSING_RESOURCE  Plan failed because a resource type is entirely absent from the HCL —
                not misconfigured, but never declared.
                → Name the missing resource type and which existing resource depends on it.

── fix_instruction rules ─────────────────────────────────────────────────────
1. Always name the exact resource label (e.g. aws_db_instance.main).
2. State the exact attribute or block to add/change and its value. When the fix
   requires adding a new resource or block, include ALL required arguments with
   concrete values — do not leave any required argument to be inferred. Incomplete
   additions will fail in the next validation round and waste retry budget.
3. MISSING_RESOURCE: name the resource type to add, which resource references it,
   and ALL required arguments with concrete values.
4. Only reference resource labels present in GENERATED HCL RESOURCES, except for MISSING_RESOURCE.
5. Return ONLY raw JSON. No markdown, no explanation.\
"""

TOP_PROMPT = "Terraform configuration failed. Classify and fix:\n\n"

BOTTOM_PROMPT = "\nOutput JSON with error_type and fix_instruction only."

# ── Error-handling prompts (Agent 4) ──────────────────────────────────────────
# Các template dưới đây lồng giữa TOP_PROMPT/BOTTOM_PROMPT (hoặc gửi thẳng cho Agent 3
# làm fix_instruction). Dữ liệu nội suy qua str.format — giá trị thay vào KHÔNG bị format
# lại nên ngoặc {} trong HCL/JSON an toàn.

# terraform validate fail → context phân loại SYNTAX.
SYNTAX_CONTEXT = (
    "TERRAFORM VALIDATE FAILED (fix EVERY error below in ONE revision):\n"
    "{validate_err}\n\n"
    "{code_context}"
    "GENERATED HCL RESOURCES: {labels}\n"
    "ERROR HISTORY (types only): {history}\n"
    "{prev_fixes}"
)
# Khối code-context lồng vào SYNTAX_CONTEXT khi trích được (rỗng nếu không).
FAILING_CODE_CONTEXT = (
    "FAILING CODE CONTEXT (one block per error, '>>>' marks the line):\n{code_ctx}\n\n"
)
# Fallback fix khi LLM không sinh được fix cho lỗi validate.
SYNTAX_FIX_FALLBACK = "terraform validate failed — fix ALL these errors: {err}"
# fix_instruction khi terraform init fail vì lỗi trong HCL.
INIT_FIX = "terraform init failed — fix the HCL:\n{err}"

# terraform plan fail (validate đã passed) → context phân loại LOGIC/MISSING_RESOURCE.
PLAN_CONTEXT = (
    "TERRAFORM VALIDATE: passed\nTERRAFORM PLAN: FAILED\n{plan_err}\n\n"
    "GENERATED HCL RESOURCES: {labels}\n"
    "{failing_resource_body}"
    "ERROR HISTORY (types only): {history}\n"
    "{prev_fixes}"
)

# Checkov pass nhưng còn security best-practice chưa đạt → fix_instruction gửi thẳng A3.
# Mô tả NGÔN NGỮ NGƯỜI (tên check), không phải CKV ID → A3 implement tự nhiên theo schema.
# Pool gồm CẢ check tier-0 (sửa in-place) LẪN graph tier-1 (companion: PAB/SSE/versioning).
# Nhiều check provider ~> 5.0 (S3 encryption/versioning/public-access) CHỈ thỏa được bằng
# resource companion riêng → KHÔNG cấm thêm block; bám ladder H1/H2/H4 của engineering.py:
# ưu tiên in-place, thêm CONFIGURATION companion khi không biểu diễn in-place được, không
# thêm service/functional resource ngoài item, không đụng phần không liên quan.
SECURITY_FIX = (
    "These security checks are not yet satisfied. Fix EACH item following your "
    "hardening rules. Do not change anything unrelated:\n{items}"
)
