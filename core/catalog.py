"""Runtime helpers cho catalog.json — dùng chung bởi agents.

catalog.json được sinh bởi core/build_catalog.py từ Checkov registry và
graph_checks YAML. Cấu trúc:
  {
    "aws_s3_bucket": [
      {"id": "CKV_AWS_19", "name": "Ensure all data stored in S3 is encrypted", "cat": ["ENCRYPTION"]},
      {"id": "CKV2_AWS_6", "name": "S3 Public Access block", "cat": ["NETWORKING"],
       "connected_types": ["aws_s3_bucket_public_access_block"]}
    ],
    ...
  }

connected_types (chỉ có ở graph checks): resource companion cần thêm vào HCL
để check đánh giá được. Không có = check tự đánh giá trên resource đó.

Chạy lại khi nâng Checkov: `python -m core.build_catalog`
"""
import json
from pathlib import Path

_CATALOG_FILE = Path(__file__).parent / "catalog.json"


def get_check_names() -> dict[str, str]:
    """Nạp flat map {check_id → check_name} từ catalog.json.

    Dùng bởi A3 (engineering) và A4 (validation) để render fix_instruction
    bằng tên check ngôn ngữ người thay vì chỉ CKV ID — A3 implement đúng hơn.

    Gọi ở module level (1 lần khi import), không gọi lại trong loop.
    Trả {} nếu catalog.json không tồn tại (fail-safe).
    """
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    names: dict[str, str] = {}
    for checks in data.values():
        for c in checks:
            cid = c.get("id", "")
            if cid and cid not in names:
                names[cid] = c.get("name", cid)
    return names
