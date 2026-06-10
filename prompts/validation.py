# ── System prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are the Validation Agent in a Terraform generation pipeline.
A generated Terraform configuration failed checks. Classify the failure and provide a precise fix.

Output (raw JSON only):
{
  "error_type": "SYNTAX | LOGIC | MISSING_RESOURCE | UNKNOWN",
  "fix_instruction": "<specific actionable instruction>"
}

SYNTAX: structural/config errors in HCL or provider blocks.
LOGIC: plan-time value/constraint errors where the boundary is correct but an
attribute or block value is wrong.
MISSING_RESOURCE: a required dependency is absent from the architecture plan,
or a data source lookup found no object.
UNKNOWN: the error is too ambiguous to classify safely.

Return a short, concrete fix. Preserve the user's intent and exact resource
label. Do not invent resources or arguments that are not supported.
7. Return ONLY raw JSON. No markdown, no explanation.\
"""

# ── Prompt wrappers ──────────────────────────────────────────────────────────
TOP_PROMPT = "Terraform configuration failed. Classify and fix:\n\n"

BOTTOM_PROMPT = "\nOutput JSON with error_type and fix_instruction only."

VALIDATE_FIX_TEMPLATE = (
    "terraform validate failed — fix ALL errors in ONE revision:\n"
    "{validate_err}"
    "{facts}"
    "{code_ctx}"
)

SECURITY_FIX_TEMPLATE = (
    "These security checks are not yet satisfied. Fix EACH item following your "
    "hardening rules. Do not change anything unrelated:\n{items}"
)

# ── Error-handling context ───────────────────────────────────────────────────
# Prompt fragments inserted between TOP_PROMPT and BOTTOM_PROMPT. Values are
# interpolated once with str.format; embedded HCL/JSON braces are safe.

# NOTE: VALIDATE_FIX and SECURITY_FIX live in agents/validation.py because they
# are direct fix templates, not LLM classification prompts.
PLAN_CONTEXT = (
    "ORIGINAL USER REQUEST:\n{prompt}\n\n"
    "INFRASTRUCTURE PLAN:\n{plan}\n\n"
    "TERRAFORM PLAN FAILED:\n{plan_err}\n"
)
