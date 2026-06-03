"""Tập trung quản lý retry budget cho toàn pipeline.

A4 (Validation) và A5 (Deployment) đều có thể route về A3 hoặc A1.
Hai agent có counter RIÊNG — lỗi A5 là lớp mới (apply-time) không liên quan
lỗi A4 đã xử lý, nên A5 luôn có đủ budget dù A4 đã dùng hết.

Keys trong retries dict:
  val_eng     — A4 → A3 (SYNTAX/LOGIC/SECURITY)
  val_arch    — A4 → A1 (MISSING_RESOURCE)
  deploy_eng  — A5 → A3 (LOGIC_DEPLOY)
  deploy_arch — A5 → A1 (MISSING_RESOURCE_DEPLOY)
  sec         — security gate trong A4 (best-effort khi hết)
"""
from dataclasses import dataclass, field

from core.state import AgentState, RetryTracker

# ── Retry budgets — single source of truth ────────────────────────────────────
MAX_TOTAL_RETRY       = 5  # global backstop — pipeline dừng hẳn sau 5 lần fail tổng
MAX_VAL_ENG_RETRY     = 3  # A4 → A3: nhiều hơn A5 vì validate rẻ hơn apply
MAX_VAL_ARCH_RETRY    = 2  # A4 → A1: re-plan ít thôi, thường fix trong 1-2 lần
MAX_VAL_SEC_RETRY     = 2  # security gate — hết → best-effort (không block deploy)
MAX_DEPLOY_ENG_RETRY  = 2  # A5 → A3: ít hơn A4 vì mỗi lần apply tốn tiền AWS
MAX_DEPLOY_ARCH_RETRY = 2  # A5 → A1: độc lập val_arch

# Template tracker rỗng — dùng khi agent chưa có entry trong retries dict.
# Không dùng RetryTracker() vì TypedDict() trả {} không có keys → KeyError khi đọc.
_BLANK_TRACKER: dict = {
    "count": 0,
    "last_error_type": "",
    "last_error_details": "",
    "error_history": [],
}


def increment_retry(
    state: AgentState,
    agent: str,
    error_type: str,
    error_details: str = "",
) -> None:
    """Tăng retry counter cho agent, tạo dict mới thay vì mutate in-place.

    Tại sao không mutate?
    LangGraph dựa vào node trả update dict để merge vào state. Nếu mutate nested
    dict trực tiếp, LangGraph structural sharing (checkpointing) có thể đọc
    giá trị cũ. Tạo dict mới đảm bảo node trả giá trị đúng khi return state.
    """
    old = state["retries"].get(agent) or _BLANK_TRACKER
    history = list(old["error_history"])  # copy để tránh mutate list cũ
    history.append(error_type)
    if len(history) > 5:
        history.pop(0)  # giữ 5 lỗi gần nhất cho oscillation detection
    state["retries"] = {
        **state["retries"],
        agent: {
            "count": old["count"] + 1,
            "last_error_type": error_type,
            "last_error_details": error_details,
            "error_history": history,
        },
    }
    state["total_attempts"] = state["total_attempts"] + 1


def check_retry_budget(
    state: AgentState,
    agent: str,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Kiểm tra agent còn retry budget không.

    Luôn trả True nếu agent chưa có entry (chưa retry lần nào).

    Returns:
        (can_retry, reason) — reason mô tả lý do khi can_retry=False
    """
    tracker = state["retries"].get(agent)
    if not tracker:
        return True, ""

    count = tracker["count"]
    if count >= max_retries:
        return False, f"{agent} đã retry {count}/{max_retries} lần"

    return True, ""


def detect_oscillation(
    state: AgentState,
    agent: str,
    current_error_type: str,
) -> bool:
    """Phát hiện oscillation — agent đang sửa nhưng lỗi vẫn lặp lại theo pattern.

    Gọi SAU increment_retry, nên current_error_type đã nằm ở cuối history[-1].

    3 patterns được kiểm:

    Pattern 1 — cùng lỗi 3 lần: [A, A, A]
      history[-3:] == [current, current, current]

    Pattern 2 — xoay vòng 2 loại: [A, B, A, B]
      current = B → B ở vị trí [-1] và [-3]; A ở [-2] và [-4]
      Điều kiện: history[-3] == history[-1] == B và history[-4] == history[-2] != B

    Pattern 3 — xoay vòng 3 loại: [A, B, C, A, B]
      current = B → B xuất hiện lần trước ở [-4]; 5 phần tử gần nhất có đúng 3 loại
      Điều kiện: history[-4] == current và len(set(history[-5:])) == 3

    Trả False nếu history chưa đủ dài để đánh giá.
    """
    tracker = state["retries"].get(agent)
    if not tracker:
        return False
    history = tracker["error_history"]

    if len(history) < 3:
        return False

    # Pattern 1: A→A→A
    if history[-3:] == [current_error_type] * 3:
        return True

    # Pattern 2: A→B→A→B (B = current, ở vị trí [-1] và [-3])
    if len(history) >= 4:
        if (history[-3] == history[-1] == current_error_type and
                history[-4] == history[-2] and history[-4] != current_error_type):
            return True

    # Pattern 3: A→B→C→A→B (B = current, xuất hiện cách 3 bước ở [-4])
    if len(history) >= 5:
        if (history[-4] == current_error_type and
                len(set(history[-5:])) == 3):
            return True

    return False


def get_retry_summary(state: AgentState) -> dict:
    """Trả summary retry state — dùng cho logging/debug."""
    return {
        agent: {
            "count": tracker["count"],
            "last_error": tracker["last_error_type"],
            "history": tracker["error_history"],
        }
        for agent, tracker in state["retries"].items()
    }
