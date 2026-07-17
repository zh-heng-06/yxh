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

    def call(self, path: str, method: str = "GET", data=None, raw: bool = False, expected: int | tuple[int, ...] = 200):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["X-ZhangGui-Request"] = "1"
        request = Request(self.base + path, data=body, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=45)
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
    owner.call("/api/setup", "POST", {"shopName": "自动测试店", "username": "owner", "displayName": "老板", "password": "test1234"})
    owner.call("/api/login", "POST", {"username": "owner", "password": "test1234"})
    me = owner.call("/api/me")
    check(me["role"] == "owner", "老板登录")
    status = owner.call("/api/status")
    check(status["database"] == "ok" and "printer" in status and status["lanUrl"], "运行状态接口", str(status["printer"]))

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

    owner.call(f"/api/devices/{first_id}/sell", "POST", {"salePrice": 7000, "paymentMethod": "微信", "customerNote": "第一次成交"})
    owner.call(f"/api/devices/{first_id}/sell", "POST", {"salePrice": 7000}, expected=409)
    check(True, "成交与重复出库保护")
    owner.call(f"/api/devices/{first_id}/return", "POST", {"reason": "自动测试退货", "refundAmount": 7000, "disposition": "restock"})
    second_sale = owner.call(f"/api/devices/{first_id}/sell", "POST", {"salePrice": 7100, "paymentMethod": "现金", "customerNote": "退货后再售", "giftCase": True, "giftScreenProtector": True, "giftChargingHead": False, "giftCharger": True})
    check(owner.call(f"/api/devices/{first_id}")["status"] == "sold", "退货重新入库后再次销售")

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

    owner.call("/api/users", "POST", {"username": "staff", "displayName": "测试店员", "password": "staff123", "role": "staff"}, expected=201)
    staff = Client(args.base)
    staff.call("/api/login", "POST", {"username": "staff", "password": "staff123"})
    staff_detail = staff.call(f"/api/devices/{first_id}")
    staff_ledger = staff.call(f"/api/ledger?date={today}")
    staff.call("/api/users", expected=403)
    staff.call(f"/api/sales/{second_sale['saleId']}/update", "POST", {"salePrice": 1}, expected=403)
    check("purchase_cost" not in staff_detail and "imei" not in staff_detail and "profit" not in staff_ledger["summary"] and "purchase_cost_snapshot" not in staff_ledger["rows"][0] and staff_ledger["rows"][0]["imei"].startswith("••••"), "店员成本、利润与完整IMEI隔离")

    backup = owner.call("/api/backups/create", "POST", {})
    check(any(item["name"] == backup["name"] for item in owner.call("/api/backups")), "手工备份创建与列表")
    imported = owner.call("/api/import/devices.csv", "POST", {"csv": "brand,model,storage,imei,purchaseCost,listPrice,area\nOPPO,Find X8,256GB,356000000000004,3500,4200,C柜\n"})
    check(imported["imported"] == 1 and not imported["errors"], "旧库存CSV导入")
    owner.call("/api/backups/restore", "POST", {"name": backup["name"], "confirmation": "RESTORE"})
    restored_devices = owner.call("/api/devices")
    check(len(restored_devices) == 3 and all(item["imei"] != "356000000000004" for item in restored_devices), "备份恢复演练")

    events = owner.call("/api/events")
    event_types = {item["event_type"] for item in events}
    required_events = {"intake", "edit", "photo_add", "reserve", "reservation_cancel", "repair_start", "repair_complete", "sale", "return"}
    check(required_events.issubset(event_types), "关键操作日志完整", ", ".join(sorted(required_events)))
    print("RESULT | ALL_AUTOMATED_TESTS_PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RESULT | FAILED | {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise
