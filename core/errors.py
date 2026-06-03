"""Error utilities dùng chung cho toàn pipeline.

Gồm:
  - MISSING_RESOURCE_PATTERNS: pattern detect resource không tồn tại (dùng A4 + A5)
  - matches_any: substring match case-insensitive (không dùng LLM, tất định)
  - make_fail: tạo fix_feedback dict chuẩn cho node trả về khi fail
"""

# Patterns phát hiện resource type không tồn tại hoặc dependency thiếu.
# Dùng chung A4 (terraform plan) và A5 (terraform apply) — bản merged gồm cả
# pattern AWS-specific từ apply ("no such resource", "resource cannot be found").
# Substring match → không cần exact phrase; lowercase nên không phân biệt hoa/thường.
MISSING_RESOURCE_PATTERNS = (
    "not found",
    "not exist",
    "does not exist",
    "invalid resource type",
    "unsupported",
    "unknown resource type",
    "type not defined",
    "no such resource",           # AWS apply error
    "resource cannot be found",   # AWS apply error
)


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
