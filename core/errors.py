"""Error utilities dùng chung cho toàn pipeline.

Gồm:
  - MISSING_RESOURCE_PATTERNS: pattern detect resource không tồn tại (dùng A4 + A5)
  - AUTH_PATTERNS: pattern detect credential/permission errors (dùng A4 + A5)
  - matches_any: substring match case-insensitive (không dùng LLM, tất định)
  - extract_error_facts: trích facts tất định từ error text, ground cho LLM classify
  - make_fail: tạo fix_feedback dict chuẩn cho node trả về khi fail
"""
import re as _re

# Patterns credential/permission — KHÔNG fixable bằng code, route thẳng requires_human.
# Canonical set dùng chung A4 + A5; route cụ thể do agent quyết định.
# Tại sao lowercase substring?
#   AWS không nhất quán case: "AccessDenied" | "access denied" | "accessdenied"
#   đều xảy ra tuỳ SDK version và service → lowercase + substring bắt hết.
# KHÔNG đưa "insufficient"/"forbidden" vào đây:
#   "InsufficientInstanceCapacity" là capacity tạm thời (transient, không phải permission).
AUTH_PATTERNS = (
    "no valid credential",
    "nocredentialproviders",
    "could not load credentials",
    "expired token",
    "invalidclienttokenid",
    "authfailure",
    "unauthorizedoperation",
    "accessdenied",
    "access denied",          # AWS thực tế trả cả hai dạng: "AccessDenied" và "Access Denied"
    "not authorized",
    "operationnotpermitted",
    "requesterror",
    "failed to instantiate provider",  # provider config sai ở plan-time
    "could not load plugin",           # provider binary thiếu ở plan-time
)

# Patterns phát hiện resource TYPE không tồn tại hoặc dependency thiếu → A1 re-plan.
# Dùng chung A4 (terraform plan) và A5 (terraform apply).
# Substring match, lowercase.
#
# KHÔNG dùng bare "not found"/"not exist"/"does not exist": chúng nuốt cả lỗi giá trị
# runtime — AMI ID, key pair name, subnet ID không tồn tại trong account → việc A3 sửa
# (LOGIC), bị misroute về A1 → đốt val_arch/deploy_arch budget vô ích.
# Tương tự bare "unsupported" — chỉ dùng cụm chỉ đúng "type không tồn tại".
# Lỗi runtime value không khớp pattern nào → qua LLM classify để phân loại đúng.
MISSING_RESOURCE_PATTERNS = (
    "invalid resource type",
    "unsupported resource type",
    "does not support resource type",   # terraform thật khi type sai
    "does not support data source",
    "unknown resource type",
    "type not defined",
    "no such resource",           # AWS: resource type/path không tồn tại
    "resource cannot be found",   # AWS apply error
)

# Patterns phát hiện HCL config sai ở terraform init (backend/required_providers block).
# Dùng trong A4 init phase để phân loại init fail → SYNTAX (fix code) vs INFRASTRUCTURE (setup).
# Substring match, lowercase.
INIT_CONFIG_ERROR_PATTERNS = (
    "backend initialization required",
    "invalid backend configuration",
    "unsupported block type",
    "invalid or missing required argument",
    "an argument named",  # unsupported argument
    "the argument",  # required argument missing
    "terraform required_providers",
    "invalid provider configuration",
    "failed to load plugin",  # provider binary/source sai
)

# Patterns phát hiện transient errors (retry-able) — dùng chung A4 + A5.
# Transient: network blip, throttle, rate limit, timeout → retry hợp lý.
# Substrate match, lowercase.
TRANSIENT_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "i/o timeout", "timed out", "context deadline exceeded",
    "tls handshake timeout", "no such host", "dial tcp",
    "reset by peer", "unexpected eof", "requesttimeout",
    "requestlimitexceeded", "throttling", "rate exceeded",
    "vpcquotaexceeded", "limitexceeded",
    "failed to query available provider packages", "registry error",  # init-specific
    # STS GetCallerIdentity rớt vì network (không phải cred sai — AUTH_PATTERNS
    # check trước nên ExpiredToken/InvalidClientTokenId vẫn vào AUTH). Các cụm này
    # xuất hiện khi mạng tới sts.<region>.amazonaws.com chập chờn → phải là TRANSIENT,
    # KHÔNG để lọt xuống LLM classify → bị gán LOGIC → A3 bịa fix (add region / remove
    # sts_endpoint) cho lỗi mạng, đốt sạch eng budget.
    "validating provider credentials", "retrieving caller identity",
    "request send failed", "statuscode: 0",
)


# ── Deterministic fact extraction ─────────────────────────────────────────────
# Các pattern dưới đây match cấu trúc cố định của terraform/AWS error messages.
# Mỗi pattern chỉ bắt khi có đủ bằng chứng văn bản — không đoán.
# Thứ tự quan trọng: pattern cụ thể trước, tổng quát sau.

_UNSUPPORTED_ARG_RE = _re.compile(r'[Aa]n argument named "([^"]+)" is not expected here')
_MISSING_ARG_RE     = _re.compile(r'[Tt]he argument "([^"]+)" is required')
_FAILED_RESOURCE_RE = _re.compile(r'\bwith ([\w]+\.[\w]+),')
_VALID_VALUES_RE    = _re.compile(r'[Vv]alid values? (?:are|is):?\s*([^\n.]{3,100})')
_MUST_ONE_OF_RE     = _re.compile(r'[Mm]ust be one of:?\s*([^\n.]{3,100})')
_NOT_ALLOWED_FOR_RE = _re.compile(r'(\w+) is not (?:allowed|valid|permitted)(?: for (\w+))?')
_INVALID_CHARS_RE   = _re.compile(r'["\']?([\w_]+)["\']? (?:attribute )?contains invalid characters?')


def extract_error_facts(error: str) -> str:
    """Trích facts tất định từ terraform/AWS error text.

    Trả về section "GROUNDED FACTS" để prepend vào LLM classify context,
    hoặc chuỗi rỗng nếu không extract được gì.

    Principle: thông tin có thể lấy từ error text bằng regex đi trước —
    LLM chỉ fill phần còn lại (classification, paraphrase, gap-fill).
    Dùng chung bởi A4 (plan errors) và A5 (apply errors).
    """
    facts: list[str] = []

    # Resource bị lỗi — "with aws_s3_bucket.main," thường có trong terraform output
    m = _FAILED_RESOURCE_RE.search(error)
    if m:
        facts.append(f"Failed resource: {m.group(1)}")

    # "An argument named X is not expected here" → luôn phải xóa X
    for m in _UNSUPPORTED_ARG_RE.finditer(error):
        facts.append(f'Action: REMOVE argument "{m.group(1)}" — does not exist in this resource schema.')

    # "The argument X is required" → luôn phải thêm X
    for m in _MISSING_ARG_RE.finditer(error):
        facts.append(f'Action: ADD required argument "{m.group(1)}".')

    # "X is not allowed/valid for Y" → giá trị X không hợp lệ
    m = _NOT_ALLOWED_FOR_RE.search(error)
    if m:
        ctx = f' for "{m.group(2)}"' if m.group(2) else ''
        facts.append(f'Invalid value: "{m.group(1)}" is not allowed{ctx}.')

    # Valid values được liệt kê trong error message — nguồn đáng tin nhất
    m = _VALID_VALUES_RE.search(error) or _MUST_ONE_OF_RE.search(error)
    if m:
        facts.append(f'Valid values (from error): {m.group(1).strip()}')

    # "X contains invalid characters" → thuộc tính X cần sanitize
    m = _INVALID_CHARS_RE.search(error)
    if m:
        facts.append(f'Attribute "{m.group(1)}" contains invalid characters — use only allowed characters.')

    if not facts:
        return ""
    lines = ["GROUNDED FACTS (extracted directly from error — anchor your fix_instruction to these):"]
    lines += [f"  - {f}" for f in facts]
    return "\n".join(lines) + "\n\n"


def matches_any(text: str, patterns: tuple) -> bool:
    """Case-insensitive substring match — tất định, không tốn LLM.

    Tại sao substring (không phải regex hay exact match)?
    Terraform/AWS error messages thường dài, lỗi thật nằm giữa text.
    Substring đủ để bắt trong mọi format output.

    Tại sao lowercase trước?
    AWS/Terraform không nhất quán case: "AccessDenied" vs "access denied" vs "accessdenied"
    đều có thể xuất hiện tuỳ context và version. Lowercase một lần, so sánh nhanh.
    """
    low = (text or "").lower()
    return any(p in low for p in patterns)


def recent_fix_instructions(history: list[dict] | None, *, limit: int = 2,
                            max_chars: int = 300, exclude: str | None = None) -> list[str]:
    """Trích các fix_instruction gần nhất từ một error-history list (anti-repeat).

    Gom phần logic trùng nhau ở A1 (arch_error_history), A3 (eng_error_history) và
    A4 (_format_prev_fixes đọc eng_error_history). Mỗi call site tự render header/bullet
    vì format khác nhau hợp lệ — helper này chỉ lo extraction:
      - lấy `limit` entry cuối,
      - bỏ entry trùng `exclude` (fix đang áp dụng — A1/A3 truyền, A4 để None),
      - truncate `max_chars` + strip, bỏ entry rỗng.

    Lưu ý: so sánh `exclude` trên giá trị GỐC (trước truncate) — giữ đúng hành vi A1/A3 cũ.
    """
    out: list[str] = []
    for e in (history or [])[-limit:]:
        raw = e.get("fix_instruction") or ""
        if raw == exclude:
            continue
        fix = raw[:max_chars].strip()
        if fix:
            out.append(fix)
    return out


def make_fail(error_type: str, root_cause: str | None, fix_instruction: str) -> dict:
    """Tạo fix_feedback dict chuẩn khi node (A1, A3) fail trước khi gọi A4.

    Dùng bởi architecture_node và engineering_node khi LLM call lỗi hoặc
    output không hợp lệ. A4 không chạy trong trường hợp này.

    Khác với _fail_return trong validation.py (A4 fail sau khi validate/plan):
      make_fail: fail TRƯỚC validation (validate_passed=False, plan_passed=False)
      _fail_return: fail SAU khi validate/plan đã chạy một phần

    error_type: INFRASTRUCTURE | MISSING_RESOURCE | SYNTAX | LOGIC | SECURITY
    root_cause: "architecture" | "engineering" | None (khi INFRASTRUCTURE)
    """
    return {
        "fix_feedback": {
            "overall_passed": False,
            "error_type": error_type,
            "root_cause": root_cause,
            "fix_instruction": fix_instruction,
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": False,
            "plan_passed": False,
        }
    }
