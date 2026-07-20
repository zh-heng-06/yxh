from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def call(self, path: str, method: str = "GET", data=None, raw: bool = False, expected: int | tuple[int, ...] = 200, timeout: int = 45, extra_headers=None):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {}
        headers.update(extra_headers or {})
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["X-ZhangGui-Request"] = "1"
        request = Request(self.base + path, data=body, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=timeout)
            status, payload = response.status, response.read()
            content_type = response.headers.get("Content-Type", "")
        except HTTPError as error:
            status, payload = error.code, error.read()
            content_type = error.headers.get("Content-Type", "")
        allowed = (expected,) if isinstance(expected, int) else expected
        if status not in allowed:
            raise AssertionError(f"{method} {path}: expected {allowed}, got {status}: {payload[:300]!r}")
        if raw:
            return payload, content_type
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))


def image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def check(condition: bool, label: str, detail="") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"PASS | {label}" + (f" | {detail}" if detail else ""), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:4191")
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()
    owner = Client(args.base)

    health = owner.call("/api/health")
    check(health["ok"] and health["databaseCheck"] == "ok", "服务与数据库自检")
    check(owner.call("/api/setup-status")["configured"] is False, "隔离测试库为空")
    owner.call("/api/setup", "POST", {"shopName": "自动测试店", "username": "owner", "displayName": "老板", "password": "test123456"})
    owner.call("/api/login", "POST", {"username": "owner", "password": "test123456"})
    me = owner.call("/api/me")
    check(me["role"] == "owner", "老板登录")
    status = owner.call("/api/status")
    check(status["version"] == "1.1.1" and status["database"] == "ok" and "printer" in status and status["lanUrl"] and status["disk"]["freeGB"] > 0, "V1.1运行状态与磁盘接口", str(status["printer"]))
    apple_status = owner.call("/api/device-connect/apple/status")
    check(isinstance(apple_status.get("available"), bool) and isinstance(apple_status.get("devices"), list) and apple_status.get("state"), "苹果USB检测接口状态明确", apple_status.get("state"))
    android_status = owner.call("/api/device-connect/android/status")
    check(isinstance(android_status.get("available"), bool) and isinstance(android_status.get("devices"), list) and android_status.get("state"), "安卓/华为USB检测接口状态明确", android_status.get("state"))
    access = owner.call("/api/access")
    desktop_html, desktop_type = owner.call("/", raw=True, extra_headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    mobile_html, mobile_type = owner.call("/", raw=True, extra_headers={"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"})
    forced_desktop, _ = owner.call("/?layout=desktop", raw=True, extra_headers={"User-Agent":"Mozilla/5.0 (iPhone) Mobile/15E148"})
    check(access["url"].endswith(":" + args.base.rsplit(":",1)[-1] + "/") and b"app.js" in desktop_html and b"mobile.js" in mobile_html and b"app.js" in forced_desktop and "text/html" in desktop_type and "text/html" in mobile_type, "同一网址自动适配电脑横屏与手机竖屏", access["url"])
    bad_login = Client(args.base)
    bad_login.call("/api/login", "POST", {"username": "owner", "password": "wrong-password"}, expected=401)
    check(True, "登录失败安全记录")

    screenshot_url = image_data_url(args.screenshot)
    ocr = owner.call("/api/devices/screenshot/recognize", "POST", {"image": screenshot_url})
    check("iPhone 16 Pro Max" in ocr.get("model", "") and ocr.get("imei") == "357507790090396", "爱思截图本地OCR", f"{ocr.get('model')} / {ocr.get('imei')}")

    invalid = {
        "brand": "Apple", "model": "Invalid", "storage": "128GB", "imei": "123", "purchaseCost": 1, "listPrice": 2
    }
    owner.call("/api/devices/intake", "POST", invalid, expected=400)
    check(True, "IMEI格式校验")
    below = {**invalid, "imei": "356000000000009", "purchaseCost": 5000, "listPrice": 4000}
    owner.call("/api/devices/intake", "POST", below, expected=409)
    check(True, "低于成本二次确认")

    devices = []
    payloads = [
        {"brand": "Apple", "model": ocr["model"], "storage": "256GB", "color": ocr.get("color", "白色钛金属"), "systemVersion": ocr.get("systemVersion", "26.4.2"), "batteryHealth": ocr.get("batteryHealth", 88), "chargeCycles": ocr.get("chargeCycles", 579), "conditionGrade": "95新", "imei": "356000000000001", "serialNumber": ocr.get("serialNumber", "C4F67P42JV"), "area": "A柜", "purchaseCost": 6400, "listPrice": 7200, "sourceFields": ocr.get("sourceFields", {})},
        {"brand": "华为", "model": "Mate 70 Pro", "storage": "512GB", "color": "黑色", "systemVersion": "HarmonyOS", "batteryHealth": 96, "conditionGrade": "99新", "imei": "356000000000002", "area": "A柜", "purchaseCost": 5200, "listPrice": 5900},
        {"brand": "小米", "model": "小米 15 Ultra", "storage": "512GB", "color": "白色", "systemVersion": "HyperOS", "batteryHealth": 98, "conditionGrade": "99新", "imei": "356000000000003", "area": "B柜", "purchaseCost": 4300, "listPrice": 4900},
    ]
    for payload in payloads:
        result = owner.call("/api/devices/intake", "POST", payload, expected=201)
        devices.append(result)
    check(len({item["stockCode"] for item in devices}) == 3, "三台入库与唯一库存编号", ", ".join(item["stockCode"] for item in devices))
    owner.call("/api/devices/intake", "POST", payloads[0], expected=409)
    check(True, "重复IMEI保护")

    first_id = devices[0]["id"]
    detail = owner.call(f"/api/devices/{first_id}")
    check(detail["purchase_cost"] == 6400 and detail["area"] == "A柜" and len(detail["photos"]) >= 1, "详情、成本与原截图留档")
    updated = owner.call(f"/api/devices/{first_id}/update", "POST", {"notes": "自动测试备注", "listPrice": 7250, "area": "A柜"})
    check(updated["changed"] >= 1, "详情编辑")
    photo = owner.call(f"/api/devices/{first_id}/photos", "POST", {"image": screenshot_url, "description": "自动测试瑕疵图"}, expected=201)
    fetched_photo, photo_type = owner.call(f"/api/photos/{photo['id']}", raw=True)
    check(len(fetched_photo) > 1000 and photo_type.startswith("image/"), "实拍照片保存与读取")

    label_png, label_type = owner.call(f"/api/devices/{first_id}/label.png", raw=True)
    check(label_png.startswith(b"\x89PNG") and label_type.startswith("image/png"), "40x30标签预览生成", f"{len(label_png)} bytes")
    qr = owner.call("/api/scan/recognize", "POST", {"image": "data:image/png;base64," + base64.b64encode(label_png).decode("ascii")})
    check(qr["value"] == devices[0]["stockCode"], "标签二维码反向识别", qr["value"])

    owner.call(f"/api/devices/{first_id}/reserve", "POST", {"customerName": "测试客户", "customerPhone": "13800000000", "deposit": 500})
    check(owner.call(f"/api/devices/{first_id}")["status"] == "reserved", "预订")
    owner.call(f"/api/devices/{first_id}/reservation/cancel", "POST", {})
    check(owner.call(f"/api/devices/{first_id}")["status"] == "in_stock", "取消预订")
    owner.call(f"/api/devices/{first_id}/repair/start", "POST", {"vendor": "测试维修商", "issue": "测试故障", "cost": 100})
    check(owner.call(f"/api/devices/{first_id}")["status"] == "in_repair", "送修")
    owner.call(f"/api/devices/{first_id}/repair/complete", "POST", {"status": "in_stock", "cost": 120, "note": "维修完成"})
    check(owner.call(f"/api/devices/{first_id}")["status"] == "in_stock", "维修完成")

    take = owner.call("/api/stocktakes/start", "POST", {"area": "A柜"}, expected=201)
    first_scan = owner.call(f"/api/stocktakes/{take['id']}/scan", "POST", {"code": devices[0]["stockCode"]})
    duplicate_scan = owner.call(f"/api/stocktakes/{take['id']}/scan", "POST", {"code": devices[0]["stockCode"]})
    owner.call(f"/api/stocktakes/{take['id']}/scan", "POST", {"code": devices[2]["stockCode"]}, expected=409)
    current = owner.call("/api/stocktakes/current")
    check(not first_scan["duplicate"] and duplicate_scan["duplicate"], "盘点重复扫描提示")
    check(current["expected"] == 2 and current["scanned"] == 1 and len(current["missing"]) == 1, "盘点漏扫与区域校验")
    owner.call(f"/api/stocktakes/{take['id']}/complete", "POST", {})
    check(owner.call("/api/stocktakes/current")["open"] is False, "完成盘点")

    first_sale = owner.call(f"/api/devices/{first_id}/sell", "POST", {"salePrice": 7000, "paymentMethod": "微信", "customerNote": "第一次成交", "handoffDisclosure": "右下角轻微磕碰", "deliveryExterior": True})
    first_token = first_sale["handoff"]["url"].split("t=", 1)[1]
    public = Client(args.base)
    first_handoff = public.call(f"/api/public/handoffs/{first_token}")
    check(first_handoff["sale"]["price"] == 7000 and first_handoff["device"]["imeiTail"] == "0001" and first_handoff["disclosure"] == "右下角轻微磕碰", "出库自动生成顾客交接快照")
    owner.call(f"/api/devices/{first_id}/sell", "POST", {"salePrice": 7000}, expected=409)
    check(True, "成交与重复出库保护")
    owner.call(f"/api/devices/{first_id}/return", "POST", {"reason": "自动测试退货", "refundAmount": 7000, "disposition": "restock"})
    public.call(f"/api/public/handoffs/{first_token}", expected=410)
    check(True, "销售退货自动作废原交接卡")
    second_sale = owner.call(f"/api/devices/{first_id}/sell", "POST", {"salePrice": 7100, "paymentMethod": "现金", "customerNote": "退货后再售", "giftCase": True, "giftScreenProtector": True, "giftChargingHead": False, "giftCharger": True, "warrantyDays": 30, "handoffDisclosure": "边框轻微使用痕迹", "handoffUnchecked": "未拆机检查内部", "deliveryExterior": True, "deliveryFunctions": True, "deliveryAccount": True, "deliveryGifts": True})
    second_token = second_sale["handoff"]["url"].split("t=", 1)[1]
    second_handoff = public.call(f"/api/public/handoffs/{second_token}")
    handoff_json = json.dumps(second_handoff, ensure_ascii=False)
    qr_svg, qr_type = public.call(f"/api/public/handoffs/{second_token}/qr.svg", raw=True)
    card_png, card_type = public.call(f"/api/public/handoffs/{second_token}/card.png", raw=True)
    check("purchase" not in handoff_json.lower() and "356000000000001" not in handoff_json and second_handoff["checklist"] and qr_svg.startswith(b"<?xml") and qr_type.startswith("image/svg") and card_png.startswith(b"\x89PNG") and card_type.startswith("image/png"), "顾客页隐私隔离、二维码与长图下载")
    reissued = owner.call(f"/api/sales/{second_sale['saleId']}/handoff/reissue", "POST", {})
    new_token = reissued["handoff"]["url"].split("t=", 1)[1]
    public.call(f"/api/public/handoffs/{second_token}", expected=404)
    check(public.call(f"/api/public/handoffs/{new_token}")["handoffNumber"] == second_handoff["handoffNumber"], "补发新交接码后旧链接失效")
    sold_detail = owner.call(f"/api/devices/{first_id}")
    check(sold_detail["status"] == "sold" and sold_detail["latestSale"]["warranty_days"] == 30 and sold_detail["latestSale"]["warranty_status"] == "active" and sold_detail["handoff"]["status"] == "active", "退货再售、30天质保与交接卡")
    after_sales = owner.call(f"/api/devices/{first_id}/after-sales", "POST", {"issue": "客户反馈充电慢"}, expected=201)
    alerts = owner.call("/api/alerts")
    check(any(item["title"] == "待处理售后" for item in alerts["items"]), "售后待办提醒")
    owner.call(f"/api/after-sales/{after_sales['id']}/resolve", "POST", {"resolution": "清理充电口后正常", "serviceCost": 20})
    resolved_detail = owner.call(f"/api/devices/{first_id}")
    check(resolved_detail["afterSales"][0]["status"] == "resolved" and resolved_detail["afterSales"][0]["service_cost"] == 20, "售后登记与处理闭环")

    dashboard = owner.call("/api/dashboard")
    check(dashboard["todaySold"] == 1 and dashboard["todayRevenue"] == 7100 and dashboard["todayProfit"] == 700, "退货冲减后的仪表盘", str(dashboard))
    today = date.today().isoformat()
    report = owner.call(f"/api/reports/summary?from={today}&to={today}")
    check(report["soldCount"] == 2 and report["refundCount"] == 1 and report["netRevenue"] == 7100 and report["netProfit"] == 700, "经营报表净收入/净利润")
    ledger = owner.call(f"/api/ledger?date={today}")
    latest = next(row for row in ledger["rows"] if row["id"] == second_sale["saleId"])
    check(ledger["summary"]["count"] == 2 and ledger["summary"]["revenue"] == 14100 and ledger["summary"]["profit"] == 1300 and latest["gift_case"] == 1 and latest["gift_screen_protector"] == 1 and latest["gift_charger"] == 1, "每日账本、利润与四类赠品")
    owner.call(f"/api/sales/{second_sale['saleId']}/update", "POST", {"salePrice": 7100, "paymentMethod": "现金", "customerNote": "账本更正测试", "giftCase": True, "giftScreenProtector": False, "giftChargingHead": True, "giftCharger": True})
    corrected = owner.call(f"/api/ledger?date={today}")
    latest = next(row for row in corrected["rows"] if row["id"] == second_sale["saleId"])
    check(latest["gift_screen_protector"] == 0 and latest["gift_charging_head"] == 1 and latest["customer_note"] == "账本更正测试", "老板更正销售账目")
    devices_csv, _ = owner.call("/api/export/devices.csv", raw=True)
    sales_csv, _ = owner.call("/api/export/sales.csv", raw=True)
    ledger_csv, _ = owner.call(f"/api/export/ledger.csv?date={today}", raw=True)
    check(devices_csv.startswith(b"\xef\xbb\xbf") and sales_csv.startswith(b"\xef\xbb\xbf") and ledger_csv.startswith(b"\xef\xbb\xbf") and "送充电头".encode() in ledger_csv, "库存、销售与每日账本CSV导出")

    parsed = owner.call("/api/smart/parse-intake", "POST", {"text": "iphone 16 pro 256G 黑色 成本6400 卖7200"})
    check(parsed["brand"] == "Apple" and parsed["storage"] == "256GB", "本地文字快速入库解析")
    suggestion = owner.call(f"/api/devices/{first_id}/price-suggestion")
    copy = owner.call(f"/api/devices/{first_id}/sales-copy")
    summary = owner.call("/api/smart/daily-summary")
    check(suggestion["mode"] == "local-data" and copy["mode"] == "local-template" and summary["mode"] == "local-data", "定价、文案和经营总结均为本地模式")

    quote_base = {"brand":"Apple","model":"iPhone 16 Pro Max","storage":"256GB","conditionGrade":"95新","batteryHealth":88,"repairStatus":"original","capturedOn":today}
    first_quote = owner.call("/api/market/quotes", "POST", {**quote_base,"sourceName":"手机回收龙猫","quoteType":"recycle","price":6200,"note":"自动测试报价"}, expected=201)
    owner.call("/api/market/quotes", "POST", {**quote_base,"sourceName":"博能二手回收","quoteType":"recycle","price":6300}, expected=201)
    owner.call("/api/market/quotes", "POST", {**quote_base,"sourceName":"测试零售平台","quoteType":"retail","price":7350}, expected=201)
    market_query = "model=iPhone%2016%20Pro%20Max&storage=256GB&conditionGrade=95%E6%96%B0&batteryHealth=88&repairStatus=original"
    market = owner.call(f"/api/market/summary?{market_query}")
    quotes = owner.call(f"/api/market/quotes?{market_query}")
    check(market["external"]["recycleCount"] == 2 and market["external"]["recycleMedian"] == 6250 and market["external"]["retailCount"] == 1 and market["internal"]["salesCount"] == 2 and len(quotes) == 3, "外部行情与门店历史独立汇总")
    decision = owner.call("/api/market/decisions", "POST", {**quote_base,"finalPurchasePrice":6250,"finalSalePrice":7200,"adjustmentReason":"同行竞争测试","suggestion":market["suggestion"],"evidence":{"external":market["external"],"internal":market["internal"]}}, expected=201)
    decisions = owner.call(f"/api/market/decisions?{market_query}")
    check(decisions[0]["id"] == decision["id"] and decisions[0]["final_purchase_price"] == 6250 and decisions[0]["final_sale_price"] == 7200, "老板最终定价独立留档")
    owner.call(f"/api/market/quotes/{first_quote['id']}/delete", "POST", {})
    check(len(owner.call(f"/api/market/quotes?{market_query}")) == 2, "错误行情删除")

    owner.call("/api/users", "POST", {"username": "staff", "displayName": "测试店员", "password": "staff12345", "role": "staff"}, expected=201)
    staff = Client(args.base)
    staff.call("/api/login", "POST", {"username": "staff", "password": "staff12345"})
    staff_detail = staff.call(f"/api/devices/{first_id}")
    staff_ledger = staff.call(f"/api/ledger?date={today}")
    staff.call("/api/users", expected=403)
    staff.call(f"/api/sales/{second_sale['saleId']}/update", "POST", {"salePrice": 1}, expected=403)
    staff.call(f"/api/market/summary?{market_query}", expected=403)
    staff.call("/api/audit-events", expected=403)
    check("purchase_cost" not in staff_detail and "imei" not in staff_detail and "purchase_cost_snapshot" not in staff_detail["latestSale"] and staff_detail["latestSale"]["imei_snapshot"].startswith("••••") and "service_cost" not in staff_detail["afterSales"][0] and "profit" not in staff_ledger["summary"] and "purchase_cost_snapshot" not in staff_ledger["rows"][0] and staff_ledger["rows"][0]["imei"].startswith("••••"), "店员成本、利润与完整IMEI隔离")

    backup = owner.call("/api/backups/create", "POST", {})
    check(any(item["name"] == backup["name"] for item in owner.call("/api/backups")), "手工备份创建与列表")
    quick = owner.call("/api/devices/quick-intake", "POST", {"model":"iPhone 13","storage":"128GB","imei":"356000000000008","purchaseCost":2200}, expected=201)
    quick_detail = owner.call(f"/api/devices/{quick['id']}")
    quick_dashboard = owner.call("/api/dashboard")
    pending_rows = owner.call("/api/devices?scope=pending_completion")
    owner.call(f"/api/devices/{quick['id']}/sell", "POST", {"salePrice":2800}, expected=409)
    check(quick_detail["intake_state"] == "pending" and quick_dashboard["pendingCompletionCount"] == 1 and pending_rows[0]["id"] == quick["id"], "忙时快速入库进入待补全且禁止出库")
    owner.call(f"/api/devices/{quick['id']}/update", "POST", {"listPrice":2800,"conditionGrade":"95新","markComplete":"on"})
    check(owner.call(f"/api/devices/{quick['id']}")["intake_state"] == "complete", "电脑端补全后解除出库锁")
    imported = owner.call("/api/import/devices.csv", "POST", {"csv": "brand,model,storage,imei,purchaseCost,listPrice,area\nOPPO,Find X8,256GB,356000000000004,3500,4200,C柜\n"})
    check(imported["imported"] == 1 and not imported["errors"], "旧库存CSV导入")
    check(any(item["action"] == "inventory_csv_import" for item in owner.call("/api/audit-events")), "批量导入审计日志")
    owner.call("/api/backups/restore", "POST", {"name": backup["name"], "confirmation": "RESTORE"})
    restored_devices = owner.call("/api/devices")
    check(len(restored_devices) == 3 and all(item["imei"] != "356000000000004" for item in restored_devices), "备份恢复演练")

    events = owner.call("/api/events")
    event_types = {item["event_type"] for item in events}
    required_events = {"intake", "edit", "photo_add", "reserve", "reservation_cancel", "repair_start", "repair_complete", "sale", "return", "after_sales_open", "after_sales_resolved"}
    check(required_events.issubset(event_types), "关键操作日志完整", ", ".join(sorted(required_events)))
    audits = owner.call("/api/audit-events")
    audit_actions = {item["action"] for item in audits}
    required_audits = {"login_failed", "device_intake", "device_update", "reservation_create", "repair_start", "device_sale", "sale_return", "sale_update", "after_sales_open", "after_sales_resolve", "backup_restore"}
    check(required_audits.issubset(audit_actions), "系统级审计日志完整", ", ".join(sorted(required_audits)))
    print("RESULT | ALL_AUTOMATED_TESTS_PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RESULT | FAILED | {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise
