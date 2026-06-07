"""Destroy helpers: cleanup terraform resources via destroy + patch deletion protection.

Shared by A4 (validation cleanup) + A5 (deployment cleanup + eval auto-destroy).
Pattern: patch deletion-protection attrs → terraform apply → terraform destroy.
"""
import logging
import re
import subprocess
import time

from core.terraform import run_terraform
from core.errors import matches_any, TRANSIENT_PATTERNS

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS: Destroy timeouts + retry budgets + deletion protection patches
# ──────────────────────────────────────────────────────────────────────────────

_DESTROY_TIMEOUT = 600   # ElastiCache/RDS cần 5-10 phút để xóa
_MAX_DESTROY_TRANSIENT_RETRY = 1  # Retry destroy nếu transient (network/throttle)
_DESTROY_RETRY_BACKOFF = 5  # giây chờ giữa các lần retry

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

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS: Patch + Destroy
# ──────────────────────────────────────────────────────────────────────────────

def patch_for_destroy(code: str) -> str:
    """Patch HCL để tắt deletion protection trước destroy.

    Áp dụng regex patterns từ _DESTROY_PATCHES để đánh bật các flag chặn delete API.
    Dùng trong eval mode hoặc manual cleanup.

    Returns: patched HCL code (nếu không match → trả nguyên gốc)
    """
    for pattern, replacement in _DESTROY_PATCHES:
        code = re.sub(pattern, replacement, code)
    return code


def destroy_resources(
    tmpdir: str,
    timeout: int = _DESTROY_TIMEOUT,
    max_retries: int = _MAX_DESTROY_TRANSIENT_RETRY,
    backoff: int = _DESTROY_RETRY_BACKOFF,
) -> tuple[bool, str | None]:
    """Execute terraform destroy với transient retry logic.

    Retry nếu transient error (network/throttle) — đối xứng terraform apply retry.
    Best-effort: nếu destroy fail → dirty state → người phải cleanup thủ công.

    Args:
        tmpdir: terraform working directory
        timeout: destroy timeout (seconds)
        max_retries: số lần retry nếu transient
        backoff: thời gian chờ trước mỗi retry (seconds)

    Returns:
        (success: bool, error_msg: str | None)
          - success=True → destroy thành công
          - success=False + error_msg=None → timeout
          - success=False + error_msg=<str> → non-transient error
    """
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(backoff * attempt)

        try:
            destroy = run_terraform(
                ["terraform", "destroy", "-auto-approve", "-no-color", "-parallelism=4"],
                tmpdir, timeout,
            )
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                logger.warning("Destroy timeout (attempt %d/%d) — retry", attempt + 1, max_retries + 1)
                continue
            return False, None  # timeout, no retry left

        if destroy.returncode == 0:
            logger.info("Destroy OK")
            return True, None

        destroy_err = (destroy.stderr or destroy.stdout or "").strip()

        # Transient error → retry nếu còn lượt
        if attempt < max_retries and matches_any(destroy_err, TRANSIENT_PATTERNS):
            logger.warning("Destroy transient (attempt %d/%d) — retry: %s",
                          attempt + 1, max_retries + 1, destroy_err[:100])
            continue

        # Non-transient error hoặc hết lượt → failed
        error_msg = destroy_err[:500]
        logger.warning("Destroy FAILED: %s", error_msg)
        return False, error_msg

    return True, None  # Shouldn't reach here, but fallback
