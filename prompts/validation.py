"""A4 Validation — classify lỗi validate/plan, sinh fix."""

SYSTEM_PROMPT = """\
You are A4 Validation Agent. Classify a Terraform validate/plan failure and give
one precise fix.

Return raw JSON only:
{
  "error_type": "SYNTAX | LOGIC | MISSING_RESOURCE | UNKNOWN",
  "fix_instruction": "<specific actionable instruction>"
}

Types:
- SYNTAX: invalid HCL/provider schema/config.
- LOGIC: boundary is correct, but value/block/relationship is wrong.
- MISSING_RESOURCE: required resource/data source is absent, or data lookup fails.
- UNKNOWN: unsafe or ambiguous.

Fix rules:
- Preserve user intent and labels.
- Name exact object and attribute/block to change.
- Suggest new resources only for MISSING_RESOURCE.
- Do not invent unsupported arguments.

Return ONLY raw JSON.\
"""

# ── Classify template ────────────────────────────────────────────────────────
# Prompt A4 classify lỗi (1 message user).
CLASSIFY_TEMPLATE = """\
Terraform failed. Classify and fix.

USER REQUEST:
{prompt}

PLAN:
{plan}

ERROR:
{plan_err}

Return JSON only.\
"""

# ── Repair templates ─────────────────────────────────────────────────────────
# Fix instruction A4 gửi cho A3 khi validate / security fail.
VALIDATE_FIX_TEMPLATE = (
    "terraform validate failed. Fix all errors in one revision:\n"
    "{validate_err}"
    "{facts}"
    "{code_ctx}"
)
SECURITY_FIX_TEMPLATE = (
    "Selected security checks are unmet. Fix each only if valid inside the "
    "existing boundary:\n{items}"
)
