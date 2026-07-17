from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "local-server"))
import server  # noqa: E402


MARKER = "演示测试数据#"
SAMPLES = [
    ("Apple", "iPhone 16 Pro Max", "256GB", "白色钛金属", "26.4.2", 88, 6400, 7250, "A柜"),
    ("Apple", "iPhone 15 Pro", "256GB", "原色钛金属", "18.5", 86, 4300, 4999, "A柜"),
    ("Apple", "iPhone 14", "128GB", "午夜色", "18.4", 84, 2850, 3399, "A柜"),
    ("华为", "Mate 70 Pro", "512GB", "曜石黑", "HarmonyOS 5", 96, 5200, 5899, "B柜"),
    ("华为", "Pura 70 Ultra", "512GB", "星芒黑", "HarmonyOS 4.2", 93, 4450, 5199, "B柜"),
    ("小米", "小米 15 Ultra", "512GB", "白色", "HyperOS 2", 98, 4300, 4899, "B柜"),
    ("小米", "小米 14", "256GB", "黑色", "HyperOS 2", 95, 2300, 2799, "C柜"),
    ("OPPO", "Find X8", "256GB", "浮光白", "ColorOS 15", 97, 3100, 3699, "C柜"),
    ("vivo", "X200 Pro", "512GB", "钛色", "OriginOS 5", 96, 3900, 4499, "C柜"),
    ("其他", "荣耀 Magic7 Pro", "512GB", "月影灰", "MagicOS 9", 94, 3700, 4299, "C柜"),
]


def next_stock_code(db, shop_id: str, brand: str) -> str:
    prefix = "A" if brand.lower() == "apple" else "H" if brand.startswith("华为") else "M"
    day = datetime.now().strftime("%Y-%m-%d")
    db.execute("insert or ignore into counters(shop_id,counter_date,prefix,last_value) values(?,?,?,0)", (shop_id, day, prefix))
    db.execute("update counters set last_value=last_value+1 where shop_id=? and counter_date=? and prefix=?", (shop_id, day, prefix))
    value = db.execute("select last_value from counters where shop_id=? and counter_date=? and prefix=?", (shop_id, day, prefix)).fetchone()[0]
    return f"{prefix}{datetime.now().strftime('%y%m%d')}-{value:03d}"


def seed() -> tuple[list[dict], Path]:
    backup = server.daily_backup(True)
    stamp = server.now_iso()
    created: list[dict] = []
    with server.WRITE_LOCK, server.connect() as db:
        actor = db.execute("select id,shop_id from users where role='owner' and active=1 order by created_at limit 1").fetchone()
        if not actor:
            raise RuntimeError("正式库没有可用的老板账号")
        existing = db.execute("select count(*) from devices where notes like ?", (f"%{MARKER}%",)).fetchone()[0]
        if existing:
            raise RuntimeError(f"已经存在{existing}条演示测试数据，为避免重复，本次没有继续添加")
        db.execute("begin immediate")
        for index, (brand, model, storage, color, system_version, battery, cost, price, area) in enumerate(SAMPLES, 1):
            device_id = str(uuid.uuid4())
            stock_code = next_stock_code(db, actor["shop_id"], brand)
            imei = f"869900260717{index:03d}"
            note = f"{MARKER}{index:02d}，请勿作为真实库存"
            db.execute(
                """insert into devices(id,shop_id,stock_code,brand,model,storage,color,system_version,battery_health,charge_cycles,condition_grade,list_price,imei,area,notes,source_fields,created_by,updated_by,created_at,updated_at)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (device_id, actor["shop_id"], stock_code, brand, model, storage, color, system_version, battery, 100 + index * 11, "95新", price, imei, area, note, json.dumps({"source":"demo_seed_20260717","test":True}, ensure_ascii=False), actor["id"], actor["id"], stamp, stamp),
            )
            db.execute("insert into device_financials(device_id,shop_id,purchase_cost,minimum_price,updated_by,updated_at) values(?,?,?,?,?,?)", (device_id, actor["shop_id"], cost, cost + 200, actor["id"], stamp))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?)", (actor["shop_id"], device_id, "intake", "in_stock", note, actor["id"], stamp))
            created.append({"id": device_id, "stockCode": stock_code, "model": model, "imei": imei})
        db.commit()
    return created, backup


def print_first(device_id: str) -> dict:
    state = server.printer_state()
    if not state.get("connected"):
        raise RuntimeError(f"打印机当前{state.get('status', '未连接')}")
    stamp = server.now_iso()
    job_id = str(uuid.uuid4())
    with server.connect() as db:
        device = db.execute("select * from devices where id=?", (device_id,)).fetchone()
        actor = db.execute("select id from users where role='owner' and active=1 order by created_at limit 1").fetchone()
        if not device or not actor:
            raise RuntimeError("找不到测试设备或老板账号")
        dummy = object.__new__(server.StoreHandler)
        payload = dummy.label_payload(device)
        db.execute("insert into print_jobs(id,shop_id,device_id,payload,status,attempts,requested_by,requested_at) values(?,?,?,?,?,?,?,?)", (job_id, device["shop_id"], device_id, json.dumps(payload, ensure_ascii=False), "printing", 1, actor["id"], stamp))
    try:
        preview = dummy.run_label_printer(device, True)
    except Exception as error:
        with server.connect() as db:
            db.execute("update print_jobs set status='failed',error_message=?,finished_at=? where id=?", (str(error), server.now_iso(), job_id))
        raise
    with server.connect() as db:
        db.execute("update print_jobs set status='printed',finished_at=? where id=?", (server.now_iso(), job_id))
    return {"jobId": job_id, "stockCode": device["stock_code"], "model": device["model"], "preview": str(preview)}


if __name__ == "__main__":
    devices, backup_path = seed()
    print("BACKUP", backup_path)
    for item in devices:
        print("ADDED", item["stockCode"], item["model"], item["imei"])
    result = print_first(devices[0]["id"])
    print("PRINTED", json.dumps(result, ensure_ascii=False))
