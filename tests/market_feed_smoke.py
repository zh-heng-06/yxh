from __future__ import annotations

import argparse
import sys
from urllib.parse import urlencode

from integration_smoke import Client, check


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:4201")
    args = parser.parse_args()
    owner = Client(args.base)
    owner.call("/api/setup", "POST", {"shopName":"官网行情测试店","username":"owner","displayName":"老板","password":"test123456"})
    owner.call("/api/login", "POST", {"username":"owner","password":"test123456"})

    status = owner.call("/api/market/feed/status")
    check(status["enabled"] and len(status["pages"]) == 2, "官网每日行情配置与老板状态接口")

    no_warranty = owner.call("/api/market/feed/sync", "POST", {"pageId":"5042"}, timeout=300)
    check(no_warranty["status"] == "success" and no_warranty["rowCount"] == 101 and no_warranty["quoteCount"] == 505, "5042苹果无保完整官网同步", str(no_warranty))
    query = urlencode({"model":"iPhone 16 PRO MAX","storage":"512GB"})
    rows = owner.call(f"/api/market/quotes?{query}")
    prices = {row["condition_grade"]:row["price"] for row in rows}
    check(prices == {"靓机":6630.0,"小花":6430.0,"大花":5450.0,"外爆":5300.0,"内爆可测":4400.0}, "16 Pro Max 512GB不再漏行且五档正确", str(prices))
    repeated = owner.call("/api/market/feed/sync", "POST", {"pageId":"5042"}, timeout=30)
    check(repeated["status"] == "unchanged", "同一官网原图不重复OCR和导入")

    warranty = owner.call("/api/market/feed/sync", "POST", {"pageId":"5041"}, timeout=300)
    check(warranty["status"] == "success" and warranty["rowCount"] == 43 and warranty["quoteCount"] == 245, "5041苹果有保六档完整官网同步", str(warranty))
    final_status = owner.call("/api/market/feed/status")
    check(all(page["lastRun"] and page["lastRun"]["status"] == "success" for page in final_status["pages"]), "两个每日行情源状态可追溯")
    print("RESULT | MARKET_FEED_TESTS_PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RESULT | FAILED | {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise
