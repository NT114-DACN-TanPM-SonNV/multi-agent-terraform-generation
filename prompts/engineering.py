"""A3 Engineering — plan + security → Terraform HCL."""

SYSTEM_PROMPT = """\
You are A3 Engineering Agent. Convert the JSON plan to deployable Terraform HCL
and apply selected security checks only when valid inside the existing plan
boundary.

Return raw HCL only:
- terraform required_providers with aws "~> 5.0"
- provider "aws"
- all planned data blocks
- all planned resource blocks

Rules:
- Emit every planned object exactly once; keep kind/type/name unchanged.
- attributes -> `arg = value`; blocks -> `name { ... }`.
- REF strings become unquoted references.
- Use depends_on only when no REF can express ordering.
- Do not add/remove/replace resources, data sources, modules, backend, variables,
  random helpers, IAM companions, SGs, fake refs, or workaround objects.
- Implement security only through valid attributes/blocks on existing resources.
- If a check or dependency needs new resources or unsupported schema, leave it unmet.
- Preserve explicit user values.
- For constrained names, use provider-native prefix fields when available; do not
  add random/helper resources.

Return ONLY raw HCL.\

"""

# ── User template ────────────────────────────────────────────────────────────
USER_TEMPLATE = """\
Plan:
{PLAN}

Security checks:
{SECURITY_CONTEXT}\
"""

# ── Repair templates ─────────────────────────────────────────────────────────
# A4/A5 route về A3 → patch tăng dần (sửa đúng chỗ, giữ code cũ, đừng lặp lỗi).
PATCH_HEADER = (
    "Your previous HCL had an error. Make ONLY this fix; keep everything else "
    "unchanged.\n\nFIX:\n"
)
PREV_CODE_HEADER = "\n\nPREVIOUS CODE:\n"
PREV_ERRORS_HEADER = "\n\nPREVIOUS ERRORS TO AVOID:\n"

# Retry in-node khi HCL vi phạm boundary plan (thêm/thiếu/trùng resource).
BOUNDARY_RETRY = """\
Your HCL violates the Architecture boundary.

Defects:
{defects}

Return complete corrected HCL. Emit exactly the planned resources/data sources.
Do not add modules, backend, helpers, or unplanned dependencies.\
"""

# Retry in-node khi output không có resource block.
NO_RESOURCE_RETRY = (
    'No `resource "` blocks found. Return complete HCL with all planned resources.'
)
