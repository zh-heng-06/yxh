from __future__ import annotations

import argparse
import sys
from urllib.parse import urlencode

from integration_smoke import Client, check


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:4197")
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    owner = Client(args.base)
    owner.call("/api/setup", "POST", {"shopName":"报价表测试店","username":"owner","displayName":"老板","password":"test123456"})
    owner.call("/api/login", "POST", {"username":"owner","password":"test123456"})
    result = owner.call("/api/market/sheet/recognize", "POST", {"sourceName":"博能二手回收","imageUrl":args.url}, timeout=300)
    check(result["capturedOn"] == "2026-07-18" and result["complete"] and result["rowCount"] == result["expectedRowCount"] == 101 and result["quoteCount"] == 505, "长报价表下载、逐格OCR与完整性校验", f"{result['rowCount']}行/{result['quoteCount']}价")

    target = next((row for row in result["rows"] if row["model"].upper() == "IPHONE 16 PRO MAX" and row["storage"] == "256GB"), None)
    check(target is not None and target["prices"].get("靓机") == 5930 and target["prices"].get("内爆可测") == 3700, "关键报价行识别准确", str(target))
    target_512 = next((row for row in result["rows"] if row["model"].upper() == "IPHONE 16 PRO MAX" and row["storage"] == "512GB"), None)
    check(target_512 is not None and target_512["prices"].get("靓机") == 6630 and target_512["prices"].get("内爆可测") == 4400, "512GB容量行不再缺失", str(target_512))

    selected = result["rows"][:8]
    expected = sum(len(row["prices"]) for row in selected)
    payload = {"sourceName":result["sourceName"],"imageUrl":result["imageUrl"],"sheetRef":result["sheetRef"],"capturedOn":result["capturedOn"],"rows":selected}
    imported = owner.call("/api/market/sheet/import", "POST", payload, expected=201)
    check(imported["imported"] == expected and imported["skipped"] == 0, "人工确认后批量写入行情库", str(imported))
    repeated = owner.call("/api/market/sheet/import", "POST", payload, expected=201)
    check(repeated["imported"] == 0 and repeated["skipped"] == expected, "同日同价重复导入保护", str(repeated))

    query = urlencode({"model":"iPhone 16 PRO MAX","storage":"256GB"})
    quotes = owner.call(f"/api/market/quotes?{query}")
    check(len(quotes) == 5 and {row["condition_grade"] for row in quotes} == {"靓机","小花","大花","外爆","内爆可测"}, "五档成色行情可查询")
    summary_query = urlencode({"model":"iPhone 16 PRO MAX","storage":"256GB","conditionGrade":"95新"})
    summary = owner.call(f"/api/market/summary?{summary_query}")
    check(summary["external"]["recycleCount"] == 1 and summary["external"]["recycleMedian"] == 5930, "店内95新自动匹配报价表靓机档")
    print("RESULT | MARKET_SHEET_TESTS_PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RESULT | FAILED | {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise
