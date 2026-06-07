import argparse
import csv
import json
import sys
from pathlib import Path

# Ép hệ thống xuất text ngay lập tức, không lưu đệm (Chống im re)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from core.metrics import rate


def _load_dataset(csv_path: Path) -> dict[int, dict]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {i: row for i, row in enumerate(rows)}


def _code_of(row: dict) -> str:
    engi = row.get("engi") or {}
    return engi.get("generated_code") or ""


def score_single_case(run_row: dict, gold: dict, row_id: int, run_name: str) -> dict:
    """Tính toán nhanh bảo mật (Bản phẳng - Chống treo cứng - Có log tiến độ)."""
    val = run_row.get("val") or {}
    code = _code_of(run_row)
    plan_valid = bool(val.get("plan_ok"))

    res = {
        "plan_valid": plan_valid,
        "score": None,
        "ratio": "0/0",
        "status": "OK",
        "rules": {}
    }

    if not code.strip():
        res["status"] = "EMPTY"
        return res
    if not plan_valid:
        res["status"] = "INVALID_PLAN"
        return res

    # Log nhẹ để bạn biết script vẫn đang chạy, không bị đơ
    print(f"   [Quét Checkov] Đang xử lý Row {row_id} của {run_name}...", end="", flush=True)

    try:
        from core.terraform import run_checkov_on_hcl
        # Hạ timeout xuống 15s để nếu gặp case lỗi tự động bỏ qua nhanh
        ck = run_checkov_on_hcl(code, timeout=15, check_ids=None)
        
        passed = ck.get("passed_count", 0)
        failed = ck.get("failed_count", 0)
        total = passed + failed
        
        res["ratio"] = f"{passed}/{total}"
        if total > 0:
            res["score"] = rate(passed, total)
        else:
            res["status"] = "NO_CHECKS"

        # Bóc tách bằng vòng lặp phẳng an toàn (Flat parsing)
        raw_passed = ck.get("passed_checks") or []
        raw_failed = ck.get("failed_checks") or []

        # Nếu bọc trong result
        if not raw_passed and "results" in ck and isinstance(ck["results"], dict):
            raw_passed = ck["results"].get("passed_checks") or []
            raw_failed = ck["results"].get("failed_checks") or []

        # Điền dữ liệu Rule ID
        if isinstance(raw_passed, list):
            for check in raw_passed:
                if isinstance(check, dict) and "check_id" in check:
                    res["rules"][check["check_id"]] = "PASS"
                elif isinstance(check, str):
                    res["rules"][check] = "PASS"

        if isinstance(raw_failed, list):
            for check in raw_failed:
                if isinstance(check, dict) and "check_id" in check:
                    res["rules"][check["check_id"]] = "FAIL"
                elif isinstance(check, str):
                    res["rules"][check] = "FAIL"
                    
        print(" Xong!")
    except Exception as e:
        res["status"] = "ERROR"
        print(" Lỗi/Timeout (Bỏ qua)!")
    return res


def main():
    ap = argparse.ArgumentParser(description="So sánh trực diện Rule bảo mật chung giữa 2 Run")
    ap.add_argument("run1", help="Đường dẫn file kết quả Run 1 (F1)")
    ap.add_argument("run2", help="Đường dẫn file kết quả Run 2 (F2)")
    ap.add_argument("--csv", required=True, help="Đường dẫn đến file dataset CSV gốc")
    args = ap.parse_args()

    print("[1/3] Đang nạp danh sách dataset gốc...")
    gold_dataset = _load_dataset(Path(args.csv))
    
    print("[2/3] Đang đọc cấu trúc 2 file kết quả JSON...")
    run1_data = {r["row"]: r for r in json.loads(Path(args.run1).read_text(encoding="utf-8")) if "row" in r}
    run2_data = {r["row"]: r for r in json.loads(Path(args.run2).read_text(encoding="utf-8")) if "row" in r}

    all_rows = sorted(list(set(run1_data.keys()).union(set(run2_data.keys()))))

    f1_name = Path(args.run1).name[:15]
    f2_name = Path(args.run2).name[:15]

    print("[3/3] Bắt đầu phân tích Checkov từng Case (Quá trình này có thể mất vài phút)...")
    print("-" * 115)

    # Lưu trữ kết quả đã quét để in bảng một lượt cho đẹp
    computed_rows = []

    for row_id in all_rows:
        gold = gold_dataset.get(row_id, {})
        diff_str = (gold.get("Difficulty") or "?").strip()

        s1_val = {"score": None, "ratio": "N/A", "status": "MISSING", "rules": {}}
        if row_id in run1_data:
            s1_val = score_single_case(run1_data[row_id], gold, row_id, f1_name)

        s2_val = {"score": None, "ratio": "N/A", "status": "MISSING", "rules": {}}
        if row_id in run2_data:
            s2_val = score_single_case(run2_data[row_id], gold, row_id, f2_name)

        computed_rows.append((row_id, diff_str, s1_val, s2_val))

    # ─── IN BẢNG ĐỐI CHIẾU CUỐI CÙNG ───
    print("\n" + "=" * 115)
    print(f"BẢNG ĐỐI CHIẾU TIÊU CHUẨN SECURITY VÀ CHI TIẾT RULE CHUNG")
    print("=" * 115)

    header_fmt = " {:<6} | {:<5} | {:<18} | {:<12} | {:<18} | {:<12} | {:<10}"
    row_fmt    = " {:<6} | {:<5} | {:<18} | {:<12} | {:<18} | {:<12} | {:<10}"
    
    print(header_fmt.format("Row ID", "Diff", f"Passed/Total ({f1_name})", "Score (F1)", f"Passed/Total ({f2_name})", "Score (F2)", "Độ lệch"))
    print("-" * 115)

    for row_id, diff_str, s1_val, s2_val in computed_rows:
        sc1_str = f"{s1_val['score']:.4f}" if s1_val['score'] is not None else f"[{s1_val['status']}]"
        sc2_str = f"{s2_val['score']:.4f}" if s2_val['score'] is not None else f"[{s2_val['status']}]"

        delta_str = "0.0000"
        if s1_val['score'] is not None and s2_val['score'] is not None:
            diff_score = s2_val['score'] - s1_val['score']
            delta_str = f"+{diff_score:.4f} 🟢" if diff_score > 0 else (f"{diff_score:.4f} 🔴" if diff_score < 0 else "0.0000")
        else:
            if s1_val['score'] == s2_val['score']: delta_str = "N/A"
            elif s1_val['score'] is not None: delta_str = "Vỡ F2 🔴"
            else: delta_str = "Vỡ F1 🟢"

        print(row_fmt.format(row_id, diff_str, s1_val["ratio"], sc1_str, s2_val["ratio"], sc2_str, delta_str))

        # Hiển thị Rule chung thực tế
        rules1 = s1_val["rules"]
        rules2 = s2_val["rules"]
        shared_rules = sorted(list(set(rules1.keys()).intersection(set(rules2.keys()))))
        
        if shared_rules:
            print("       └─ 🔍 So sánh Rule chung:")
            for rule_id in shared_rules:
                st1 = rules1[rule_id]
                st2 = rules2[rule_id]
                
                if st1 == "PASS" and st2 == "FAIL":
                    status_sign = "🔴 F1 tốt hơn (F2 tạch)"
                elif st1 == "FAIL" and st2 == "PASS":
                    status_sign = "🟢 F2 tốt hơn (F1 tạch)"
                elif st1 == "FAIL" and st2 == "FAIL":
                    status_sign = "🤝 Cùng tạch (Yếu bảo mật)"
                else:
                    status_sign = "✨ Cùng Pass (An toàn)"
                    
                print(f"          │  • {rule_id:<12} -> F1: {st1:<4} | F2: {st2:<4}  [{status_sign}]")
        elif s1_val["status"] == "OK" and s2_val["status"] == "OK":
            print("       └─ ⚠️  Không tìm thấy Rule chung (Chiến lược chọn tài nguyên IaC của 2 bên khác nhau)")
            
        print("." * 115)


if __name__ == "__main__":
    main()