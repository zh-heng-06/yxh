from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import hmac
import html
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT.parent / "web"
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("ZHANGGUI_DB_PATH", str(DATA_DIR / "store.db"))).resolve()
SCHEMA_PATH = ROOT / "schema.sql"
PRINT_AGENT = ROOT.parent / "print-agent" / "LabelPrinter.exe"
VENDOR = ROOT / "vendor"
VENDOR_OCR = ROOT / "vendor-ocr"
VENDOR_QR = ROOT / "vendor-qr"
if VENDOR_QR.exists():
    sys.path.insert(0, str(VENDOR_QR))
if VENDOR_OCR.exists():
    sys.path.insert(0, str(VENDOR_OCR))
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))
try:
    import qrcode
    import qrcode.image.svg
except ImportError:
    qrcode = None
try:
    import zxingcpp
except ImportError:
    zxingcpp = None
try:
    from ocr_intake import recognize_device_screenshot
except ImportError:
    recognize_device_screenshot = None
try:
    from market_ocr import recognize_market_sheet
except ImportError:
    recognize_market_sheet = None

COOKIE_NAME = "zhanggui_session"
APP_VERSION = "1.0.0"
SESSION_DAYS = 30
PBKDF2_ROUNDS = 260_000
WRITE_LOCK = threading.Lock()
BACKUP_LOCK = threading.Lock()
MARKET_FEED_LOCK = threading.Lock()
LOGIN_LOCK = threading.Lock()
LOGIN_FAILURES: dict[str, list[datetime]] = {}
MARKET_FEED_PAGES = {
    "5041": {"name":"博能二手回收·苹果有保", "url":"https://13994040400.huishoubaojia.com/5041.html", "minimumRows":20},
    "5042": {"name":"博能二手回收·苹果无保", "url":"https://13994040400.huishoubaojia.com/5042.html", "minimumRows":80},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_logger() -> logging.Logger:
    logger = logging.getLogger("zhanggui")
    if logger.handlers:
        return logger
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DATA_DIR / "system.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


LOGGER = build_logger()


def audit_insert(db: sqlite3.Connection, *, shop_id: str | None, action: str, summary: str,
                 actor: dict | None = None, entity_type: str = "", entity_id: str = "",
                 details: dict | None = None, success: bool = True, client_ip: str = "") -> None:
    """Append a privacy-limited, immutable business audit record."""
    db.execute("""insert into audit_events(shop_id,actor_id,actor_name,actor_role,action,entity_type,entity_id,summary,details,success,client_ip,created_at)
        values(?,?,?,?,?,?,?,?,?,?,?,?)""", (
        shop_id, actor.get("id") if actor else None, actor.get("display_name", "") if actor else "系统",
        actor.get("role", "") if actor else "system", action, entity_type, entity_id, summary,
        json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")), 1 if success else 0,
        client_ip, now_iso(),
    ))


class StoreConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=15, factory=StoreConnection)
    db.row_factory = sqlite3.Row
    db.execute("pragma foreign_keys=on")
    db.execute("pragma busy_timeout=15000")
    return db


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("pragma journal_mode=wal")
        db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_db()


def migrate_db() -> None:
    with connect() as db:
        applied = {row[0] for row in db.execute("select version from schema_migrations")}
        if 1 not in applied:
            definition = db.execute("select sql from sqlite_master where type='table' and name='sales'").fetchone()[0].lower()
            if "device_id text not null unique" in definition:
                db.commit(); db.execute("pragma foreign_keys=off"); db.execute("pragma legacy_alter_table=on")
                db.execute("alter table sales rename to sales_legacy")
                db.execute("""create table sales(id text primary key,shop_id text not null references shops(id),device_id text not null references devices(id),sale_price real not null check(sale_price>=0),payment_method text not null default '',customer_note text not null default '',sold_by text not null references users(id),sold_at text not null)""")
                db.execute("insert into sales select * from sales_legacy")
                db.execute("drop table sales_legacy"); db.execute("create index if not exists sales_device_idx on sales(device_id,sold_at desc)")
                db.execute("pragma foreign_keys=on")
            db.execute("insert into schema_migrations(version,applied_at) values(1,?)", (now_iso(),))
        if 2 not in applied:
            columns = {row[1] for row in db.execute("pragma table_info(sales)")}
            additions = {
                "model_snapshot": "text not null default ''",
                "storage_snapshot": "text not null default ''",
                "imei_snapshot": "text not null default ''",
                "purchase_cost_snapshot": "real not null default 0",
                "gift_case": "integer not null default 0",
                "gift_screen_protector": "integer not null default 0",
                "gift_charging_head": "integer not null default 0",
                "gift_charger": "integer not null default 0",
                "updated_at": "text",
                "updated_by": "text references users(id)",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"alter table sales add column {name} {definition}")
            db.execute("""update sales set
                model_snapshot=coalesce(nullif(model_snapshot,''),(select model from devices where id=sales.device_id),''),
                storage_snapshot=coalesce(nullif(storage_snapshot,''),(select storage from devices where id=sales.device_id),''),
                imei_snapshot=coalesce(nullif(imei_snapshot,''),(select coalesce(imei,serial_number,'') from devices where id=sales.device_id),''),
                purchase_cost_snapshot=case when purchase_cost_snapshot=0 then coalesce((select purchase_cost from device_financials where device_id=sales.device_id),0) else purchase_cost_snapshot end
            """)
            db.execute("insert into schema_migrations(version,applied_at) values(2,?)", (now_iso(),))
        if 3 not in applied:
            db.execute("insert into schema_migrations(version,applied_at) values(3,?)", (now_iso(),))
        if 4 not in applied:
            db.execute("insert into schema_migrations(version,applied_at) values(4,?)", (now_iso(),))
        if 5 not in applied:
            db.execute("insert into schema_migrations(version,applied_at) values(5,?)", (now_iso(),))
        if 6 not in applied:
            sales_columns = {row[1] for row in db.execute("pragma table_info(sales)")}
            if "warranty_days" not in sales_columns:
                db.execute("alter table sales add column warranty_days integer not null default 30 check (warranty_days between 0 and 3650)")
            if "warranty_expires_at" not in sales_columns:
                db.execute("alter table sales add column warranty_expires_at text")
            db.executescript("""
                create table if not exists after_sales_cases (
                  id text primary key, shop_id text not null references shops(id),
                  device_id text not null references devices(id), sale_id text not null references sales(id),
                  issue text not null, status text not null default 'open' check(status in ('open','resolved')),
                  resolution text not null default '', service_cost real not null default 0 check(service_cost>=0),
                  created_by text not null references users(id), closed_by text references users(id),
                  created_at text not null, updated_at text not null, closed_at text
                );
                create index if not exists after_sales_device_idx on after_sales_cases(device_id,created_at desc);
                create table if not exists audit_events (
                  id integer primary key autoincrement, shop_id text references shops(id),
                  actor_id text references users(id) on delete set null, actor_name text not null default '',
                  actor_role text not null default '', action text not null, entity_type text not null default '',
                  entity_id text not null default '', summary text not null, details text not null default '{}',
                  success integer not null default 1, client_ip text not null default '', created_at text not null
                );
                create index if not exists audit_events_shop_time_idx on audit_events(shop_id,created_at desc,id desc);
                create trigger if not exists audit_events_no_update before update on audit_events begin select raise(abort,'audit events are immutable'); end;
                create trigger if not exists audit_events_no_delete before delete on audit_events begin select raise(abort,'audit events are immutable'); end;
            """)
            db.execute("insert into schema_migrations(version,applied_at) values(6,?)", (now_iso(),))


def database_check() -> str:
    with connect() as db:
        result = db.execute("pragma quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"数据库自检失败：{result}")
    return result


def database_file_check(path: Path) -> str:
    db = sqlite3.connect(path)
    try:
        result = db.execute("pragma quick_check").fetchone()[0]
    finally:
        db.close()
    if result != "ok":
        raise RuntimeError(f"备份数据库自检失败：{result}")
    return result


def daily_backup(force: bool = False) -> Path:
    with BACKUP_LOCK:
        backup_dir = DB_PATH.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Manual, scheduled and pre-restore backups can happen close together.
        # Microseconds keep every recovery point distinct.
        suffix = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") if force else datetime.now().strftime("%Y-%m-%d")
        target = backup_dir / f"store-{suffix}.db"
        if force or not target.exists():
            source, destination = connect(), sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        try:
            database_file_check(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        cutoff = datetime.now().timestamp() - 30 * 86400
        for old in backup_dir.glob("store-*.db"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        return target


def backup_state() -> dict:
    backup_dir = DB_PATH.parent / "backups"
    backups = sorted(backup_dir.glob("store-*.db"), key=lambda item: item.stat().st_mtime, reverse=True) if backup_dir.exists() else []
    if not backups:
        return {"schedule": "每天19:00", "lastBackup": None, "verified": False, "status": "missing"}
    latest = backups[0]
    try:
        database_file_check(latest)
        verified, status = True, "ok"
    except Exception:
        verified, status = False, "invalid"
    return {
        "schedule": "每天19:00",
        "lastBackup": latest.name,
        "lastBackupAt": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "size": latest.stat().st_size,
        "verified": verified,
        "status": status,
    }


def next_daily_backup_at(now: datetime | None = None) -> datetime:
    current = now or datetime.now().astimezone()
    target = current.replace(hour=19, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target


def backup_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        current = datetime.now().astimezone()
        target = next_daily_backup_at(current)
        if stop_event.wait(max(1, (target - current).total_seconds())):
            return
        try:
            path = daily_backup(True)
            with connect() as db:
                shops = db.execute("select id from shops").fetchall()
                for shop in shops:
                    audit_insert(db, shop_id=shop["id"], action="backup_scheduled", summary=f"19:00自动备份成功：{path.name}")
            print(f"BACKUP_OK: {path.name}")
            LOGGER.info("scheduled backup succeeded name=%s", path.name)
        except Exception as error:
            print(f"BACKUP_ERROR: {error}", file=sys.stderr)
            LOGGER.exception("scheduled backup failed")


def fetch_public_market_page(page_id: str) -> dict:
    config = MARKET_FEED_PAGES.get(page_id)
    if not config:
        raise ValueError("目前只支持苹果有保和苹果无保报价页")
    page_url = config["url"]
    request = Request(page_url, headers={"User-Agent":"Mozilla/5.0 ZhangGui/1.0","Accept":"text/html"})
    with urlopen(request, timeout=25) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != "13994040400.huishoubaojia.com":
            raise ValueError("报价网页跳转到了未允许的地址")
        body = response.read(300_001)
        if len(body) > 300_000 or response.headers.get_content_type() != "text/html":
            raise ValueError("报价网页格式或大小不正确")
    text = body.decode("utf-8", errors="replace")
    image_match = re.search(r'<img\s+class=["\']image_box["\'][^>]+src=["\']([^"\']+)', text, re.I)
    if not image_match:
        raise ValueError("报价网页没有找到完整报价原图")
    image_url = html.unescape(image_match.group(1).strip())
    image = urlparse(image_url)
    if image.scheme != "https" or image.hostname != "cos.huishoubaojiadan.com":
        raise ValueError("报价网页返回了未允许的图片地址")
    return {**config, "pageId":page_id, "pageUrl":page_url, "imageUrl":image_url}


def save_market_image(image_url: str) -> tuple[Path, str]:
    request = Request(image_url, headers={"User-Agent":"Mozilla/5.0 ZhangGui/1.0","Accept":"image/png,image/jpeg,image/webp"})
    with urlopen(request, timeout=30) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != "cos.huishoubaojiadan.com":
            raise ValueError("报价原图跳转到了未允许的地址")
        content_type = response.headers.get_content_type()
        body = response.read(15_000_001)
    if len(body) > 15_000_000 or len(body) < 1_000 or content_type not in ("image/png","image/jpeg","image/webp"):
        raise ValueError("报价原图格式或大小不正确")
    if body.startswith(b"\x89PNG"):
        suffix = ".png"
    elif body.startswith(b"\xff\xd8"):
        suffix = ".jpg"
    elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise ValueError("网页返回的不是支持的报价原图")
    directory = DB_PATH.parent / "market-sheets"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4()}{suffix}"
    path.write_bytes(body)
    return path, str(path.relative_to(DB_PATH.parent)).replace("\\", "/")


def market_result_error(result: dict, minimum_rows: int, previous_rows: int = 0) -> str:
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    expected = int(result.get("expectedRowCount") or 0)
    if not result.get("complete") or len(rows) != expected:
        return f"完整性校验未通过：表格应有{expected}行，识别到{len(rows)}行"
    required = set(result.get("conditions") or ())
    if required not in ({"靓机","小花","大花","外爆","内爆可测"},{"高保充新","靓机","小花","大花","外爆","内爆可测"}):
        return "报价档位表头无法确认"
    if len(rows) < max(minimum_rows, round(previous_rows * .9)):
        return f"识别行数异常减少：本次{len(rows)}行，上次{previous_rows}行"
    seen = set()
    for row in rows:
        key = (str(row.get("model", "")).lower(), str(row.get("storage", "")).upper())
        if not all(key) or key in seen:
            return "存在空型号、空容量或重复型号容量"
        seen.add(key)
        prices = row.get("prices") if isinstance(row.get("prices"), dict) else {}
        mandatory = required - {"高保充新"}
        if not mandatory.issubset(prices) or not set(prices).issubset(required):
            return f"{row.get('model','')} {row.get('storage','')} 五档价格不完整"
        ordered_names = tuple(name for name in ("高保充新","靓机","小花","大花","外爆","内爆可测") if name in prices)
        values = [float(prices[name]) for name in ordered_names]
        if any(value < 50 or value > 50000 for value in values) or values != sorted(values, reverse=True):
            return f"{row.get('model','')} {row.get('storage','')} 价格顺序异常"
    return ""


def record_market_feed(shop_id: str, feed: dict, captured_on: str, status: str, *, image_url: str = "", file_ref: str = "", result: dict | None = None, imported: int = 0, skipped: int = 0, message: str = "") -> None:
    result = result or {}
    with WRITE_LOCK, connect() as db:
        db.execute("""insert into market_feed_runs(id,shop_id,source_name,page_id,page_url,captured_on,image_url,file_path,status,expected_row_count,row_count,quote_count,imported_count,skipped_count,message,created_at)
                      values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (str(uuid.uuid4()),shop_id,feed["name"],feed["pageId"],feed["pageUrl"],captured_on,image_url,file_ref,status,int(result.get("expectedRowCount") or 0),int(result.get("rowCount") or 0),int(result.get("quoteCount") or 0),imported,skipped,message[:300],now_iso()))


def run_market_feed_page(shop_id: str, owner_id: str, page_id: str) -> dict:
    if recognize_market_sheet is None:
        raise RuntimeError("本机行情OCR组件未安装")
    if not MARKET_FEED_LOCK.acquire(blocking=False):
        raise RuntimeError("另一个行情同步任务正在进行，请稍后再试")
    feed = MARKET_FEED_PAGES.get(page_id) or {"name":"未知报价页","pageId":page_id,"pageUrl":"","minimumRows":0}
    captured_on, image_url, file_ref, result = datetime.now().date().isoformat(), "", "", {}
    try:
        feed = fetch_public_market_page(page_id)
        image_url = feed["imageUrl"]
        timestamp = parse_qs(urlparse(image_url).query).get("ts", [""])[0]
        if re.match(r"^20\d{6}", timestamp):
            captured_on = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        with connect() as db:
            existing = db.execute("select * from market_feed_runs where shop_id=? and page_id=? and image_url=? and status='success' order by created_at desc limit 1",(shop_id,page_id,image_url)).fetchone()
            previous = db.execute("select row_count from market_feed_runs where shop_id=? and page_id=? and status='success' order by captured_on desc,created_at desc limit 1",(shop_id,page_id)).fetchone()
        if existing:
            return {"ok":True,"status":"unchanged","pageId":page_id,"sourceName":feed["name"],"capturedOn":captured_on,"message":"网页仍是已经成功导入的同一张报价表"}
        path, file_ref = save_market_image(image_url)
        result = recognize_market_sheet(path)
        error = market_result_error(result, int(feed["minimumRows"]), int(previous[0]) if previous else 0)
        if error:
            record_market_feed(shop_id,feed,captured_on,"rejected",image_url=image_url,file_ref=file_ref,result=result,message=error)
            return {"ok":False,"status":"rejected","pageId":page_id,"sourceName":feed["name"],"capturedOn":captured_on,"message":error,**{key:result.get(key) for key in ("rowCount","expectedRowCount","quoteCount")}}
        stamp, imported, skipped = now_iso(), 0, 0
        conditions = tuple(result["conditions"])
        with WRITE_LOCK, connect() as db:
            db.execute("begin immediate")
            for row in result["rows"]:
                for condition in conditions:
                    if condition not in row["prices"]:
                        continue
                    price = float(row["prices"][condition])
                    exists = db.execute("""select 1 from market_quotes where shop_id=? and source_name=? and quote_type='recycle' and lower(model)=lower(?) and lower(storage)=lower(?) and condition_grade=? and price=? and captured_on=? limit 1""",(shop_id,feed["name"],row["model"],row["storage"],condition,price,captured_on)).fetchone()
                    if exists:
                        skipped += 1; continue
                    note = f"官网每日自动导入 · {feed['pageId']}" + (f" · 网络型号{row.get('networkModel')}" if row.get("networkModel") else "")
                    db.execute("""insert into market_quotes(id,shop_id,source_name,quote_type,brand,model,storage,condition_grade,battery_health,repair_status,price,captured_on,note,created_by,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),shop_id,feed["name"],"recycle",row.get("brand","Apple"),row["model"],row["storage"],condition,None,"unknown",price,captured_on,note,owner_id,stamp))
                    imported += 1
            db.execute("""insert into market_sheet_imports(id,shop_id,source_name,captured_on,image_url,file_path,row_count,quote_count,created_by,created_at) values(?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),shop_id,feed["name"],captured_on,image_url,file_ref,len(result["rows"]),imported,owner_id,stamp))
        record_market_feed(shop_id,feed,captured_on,"success",image_url=image_url,file_ref=file_ref,result=result,imported=imported,skipped=skipped,message="完整性和价格顺序校验通过")
        return {"ok":True,"status":"success","pageId":page_id,"sourceName":feed["name"],"capturedOn":captured_on,"rowCount":result["rowCount"],"quoteCount":result["quoteCount"],"imported":imported,"skipped":skipped,"message":"完整报价已安全导入"}
    except Exception as error:
        record_market_feed(shop_id,feed,captured_on,"error",image_url=image_url,file_ref=file_ref,result=result,message=str(error))
        return {"ok":False,"status":"error","pageId":page_id,"sourceName":feed["name"],"capturedOn":captured_on,"message":str(error)[:200]}
    finally:
        MARKET_FEED_LOCK.release()


def scheduled_market_feed_cycle() -> None:
    today = datetime.now().date().isoformat()
    with connect() as db:
        owners = db.execute("select s.id shop_id,u.id owner_id from shops s join users u on u.shop_id=s.id and u.role='owner' and u.active=1").fetchall()
    for owner in owners:
        for page_id in MARKET_FEED_PAGES:
            with connect() as db:
                success = db.execute("select 1 from market_feed_runs where shop_id=? and page_id=? and captured_on=? and status='success' limit 1",(owner["shop_id"],page_id,today)).fetchone()
                latest = db.execute("select created_at from market_feed_runs where shop_id=? and page_id=? order by created_at desc limit 1",(owner["shop_id"],page_id)).fetchone()
            if success:
                continue
            if latest:
                attempted = datetime.fromisoformat(latest[0]).replace(tzinfo=None)
                if datetime.utcnow() - attempted < timedelta(minutes=55):
                    continue
            run_market_feed_page(owner["shop_id"],owner["owner_id"],page_id)


def market_feed_scheduler(stop_event: threading.Event) -> None:
    stop_event.wait(10)
    while not stop_event.is_set():
        now = datetime.now()
        if (now.hour, now.minute) >= (14, 30):
            try:
                scheduled_market_feed_cycle()
            except Exception as error:
                print(f"MARKET_FEED_ERROR: {error}", file=sys.stderr)
        stop_event.wait(300)


def printer_state() -> dict:
    command = ["powershell.exe", "-NoProfile", "-Command", "$p=Get-Printer -Name 'NIIMBOT B1' -ErrorAction Stop; $w=Get-CimInstance Win32_Printer -Filter \"Name='NIIMBOT B1'\" -ErrorAction Stop; [pscustomobject]@{Name=$p.Name;PrinterStatus=[string]$p.PrinterStatus;PortName=$p.PortName;WorkOffline=[bool]$w.WorkOffline;JobCount=[int]$p.JobCount} | ConvertTo-Json -Compress"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6)
        if result.returncode != 0:
            return {"connected": False, "status": "未连接"}
        payload = json.loads(result.stdout.strip().lstrip("\ufeff"))
        printer_status = str(payload.get("PrinterStatus", ""))
        offline = bool(payload.get("WorkOffline"))
        connected = not offline and printer_status.lower() in ("normal", "idle", "printing", "0", "3", "4")
        return {"connected": connected, "status": "离线" if offline else ("正常" if connected else printer_status or "状态未知"), "port": payload.get("PortName", ""), "workOffline": offline, "jobCount": int(payload.get("JobCount") or 0)}
    except Exception:
        return {"connected": False, "status": "检测失败"}


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return digest.hex(), salt.hex()


def verify_password(password: str, expected: str, salt_hex: str) -> bool:
    actual, _ = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(actual, expected)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def rowdict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def median_value(values: list[float]) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def rounded_range(center: float | None, spread: float) -> list[float] | None:
    if center is None or center <= 0:
        return None
    return [max(0, round(center * (1 - spread) / 10) * 10), round(center * (1 + spread) / 10) * 10]


def decode_qr_image(image_bytes: bytes) -> str:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return ""
    if zxingcpp is not None:
        try:
            results = zxingcpp.read_barcodes(
                image,
                formats=zxingcpp.BarcodeFormat.QRCode,
                try_rotate=True,
                try_downscale=True,
                try_invert=True,
            )
            for result in results:
                value = str(result.text or "").strip()
                if value:
                    return value
        except Exception:
            pass
    detector = cv2.QRCodeDetector()
    candidates = [image]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    candidates.extend([
        gray,
        cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
        cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE),
        cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE),
        cv2.rotate(image, cv2.ROTATE_180),
    ])
    for candidate in candidates:
        try:
            value, _, _ = detector.detectAndDecode(candidate)
            if value.strip():
                return value.strip()
        except Exception:
            continue
    return ""


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class StoreHandler(SimpleHTTPRequestHandler):
    server_version = "ZhangGuiLocal/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; media-src 'self' blob:")
        super().end_headers()

    @property
    def client_ip(self) -> str:
        return str(self.client_address[0])[:64]

    @property
    def path_only(self) -> str:
        return unquote(urlparse(self.path).path)

    @property
    def phone_url(self) -> str:
        return f"http://{lan_ip()}:{self.server.server_port}/"

    def send_json(self, payload, status=HTTPStatus.OK, cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 手机切换网络或浏览器超时后，OCR等本地任务可能刚好完成。
            # 客户端已经离开时无需再次向断开的连接写错误响应。
            return

    def read_json(self, max_bytes=1_000_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > max_bytes: raise ValueError
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict): raise ValueError
            return value
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求数据格式不正确")

    def current_user(self, required=True) -> dict | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get(COOKIE_NAME)
        if not token:
            if required: raise ApiError(HTTPStatus.UNAUTHORIZED, "请先登录")
            return None
        token_hash = hashlib.sha256(token.value.encode()).hexdigest()
        with connect() as db:
            row = db.execute("""
                select u.id,u.shop_id,u.username,u.display_name,u.role,s.name shop_name
                from sessions x join users u on u.id=x.user_id join shops s on s.id=u.shop_id
                where x.token_hash=? and x.expires_at>? and u.active=1
            """, (token_hash, now_iso())).fetchone()
            if row: db.execute("update sessions set last_seen_at=? where token_hash=?", (now_iso(), token_hash))
        if not row and required: raise ApiError(HTTPStatus.UNAUTHORIZED, "登录已过期")
        return rowdict(row)

    def require_write_header(self) -> None:
        if self.headers.get("X-ZhangGui-Request") != "1":
            raise ApiError(HTTPStatus.FORBIDDEN, "请求校验失败")

    def do_GET(self) -> None:
        try:
            path = self.path_only
            if path == "/api/setup-status":
                with connect() as db: configured = db.execute("select exists(select 1 from users)").fetchone()[0] == 1
                return self.send_json({"configured": configured})
            if path == "/api/access":
                return self.send_json({"url": self.phone_url, "mode": "local-sqlite"})
            if path == "/api/qrcode.svg":
                return self.send_qr()
            if path == "/api/me":
                return self.send_json(self.current_user())
            if path == "/api/status":
                user = self.current_user()
                disk = shutil.disk_usage(DB_PATH.parent)
                return self.send_json({"version": APP_VERSION, "database": database_check(), "printer": printer_state(), "lanUrl": self.phone_url, "backupRetentionDays": 30, "backup": backup_state(), "role": user["role"], "disk": {"freeGB": round(disk.free / 1024**3, 1), "freePercent": round(disk.free / disk.total * 100, 1), "ok": disk.free >= 5 * 1024**3 and disk.free / disk.total >= .1}})
            if path == "/api/backups":
                return self.send_json(self.list_backups(self.current_user()))
            if path == "/api/users":
                return self.send_json(self.list_users(self.current_user()))
            if path == "/api/dashboard":
                return self.send_json(self.dashboard(self.current_user()))
            if path == "/api/devices":
                return self.send_json(self.list_devices(self.current_user()))
            if path.startswith("/api/devices/") and path.count("/") == 3:
                return self.send_json(self.device_detail(self.current_user(), path.split("/")[3]))
            if path == "/api/events":
                return self.send_json(self.list_events(self.current_user()))
            if path == "/api/audit-events":
                return self.send_json(self.list_audit_events(self.current_user()))
            if path == "/api/alerts":
                return self.send_json(self.operations_alerts(self.current_user()))
            if path == "/api/reports/summary":
                return self.send_json(self.report_summary(self.current_user()))
            if path == "/api/ledger":
                return self.send_json(self.daily_ledger(self.current_user()))
            if path == "/api/market/quotes":
                return self.send_json(self.list_market_quotes(self.current_user()))
            if path == "/api/market/summary":
                return self.send_json(self.market_summary(self.current_user()))
            if path == "/api/market/decisions":
                return self.send_json(self.list_pricing_decisions(self.current_user()))
            if path == "/api/market/feed/status":
                return self.send_json(self.market_feed_status(self.current_user()))
            if path == "/api/smart/daily-summary":
                return self.send_json(self.smart_daily_summary(self.current_user()))
            if path.startswith("/api/devices/") and path.endswith("/price-suggestion"):
                return self.send_json(self.price_suggestion(self.current_user(), path.split("/")[3]))
            if path.startswith("/api/devices/") and path.endswith("/sales-copy"):
                return self.send_json(self.sales_copy(self.current_user(), path.split("/")[3]))
            if path == "/api/export/devices.csv":
                return self.export_devices(self.current_user())
            if path == "/api/export/sales.csv":
                return self.export_sales(self.current_user())
            if path == "/api/export/ledger.csv":
                return self.export_ledger(self.current_user())
            if path == "/api/import/template.csv":
                self.current_user(); return self.send_csv("device-import-template.csv",["品牌","型号","容量","颜色","系统版本","电池健康","充电次数","成色","IMEI","IMEI2","序列号","库位","标价","收货成本","备注"],[["Apple","iPhone 16 Pro","256GB","黑色","26.4.2","90","120","95新","请替换为15位IMEI","","","A柜","7200","6400",""]])
            if path == "/api/stocktakes/current":
                return self.send_json(self.current_stocktake(self.current_user()))
            if path.startswith("/api/devices/") and path.endswith("/label.png"):
                return self.send_label_preview(self.current_user(), path.split("/")[3])
            if path.startswith("/api/photos/"):
                return self.send_device_photo(self.current_user(), path.split("/")[3])
            if path == "/api/health":
                return self.send_json({"ok": True, "version": APP_VERSION, "database": str(DB_PATH), "databaseCheck": database_check(), "time": now_iso()})
            super().do_GET()
        except ApiError as error:
            self.send_json({"error": str(error)}, error.status)
        except Exception as error:
            print(f"API_ERROR {self.path}: {error}", file=sys.stderr)
            LOGGER.exception("GET failed path=%s ip=%s", self.path, self.client_ip)
            self.send_json({"error": "系统读取失败，请查看电脑服务窗口"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            self.require_write_header()
            path = self.path_only
            if path == "/api/setup": return self.first_setup(self.read_json())
            if path == "/api/login": return self.login(self.read_json())
            if path == "/api/logout": return self.logout()
            if path == "/api/password": return self.change_password(self.current_user(), self.read_json())
            if path == "/api/backups/create": return self.create_backup(self.current_user())
            if path == "/api/backups/restore": return self.restore_backup(self.current_user(), self.read_json())
            if path == "/api/devices/screenshot/recognize":
                return self.recognize_screenshot(self.current_user(), self.read_json(15_000_000))
            if path == "/api/scan/recognize":
                return self.recognize_qr(self.current_user(), self.read_json(15_000_000))
            if path == "/api/smart/parse-intake":
                return self.send_json(self.parse_intake_text(self.current_user(), self.read_json()))
            if path == "/api/market/quotes":
                return self.create_market_quote(self.current_user(), self.read_json())
            if path == "/api/market/sheet/recognize":
                return self.recognize_market_sheet_url(self.current_user(), self.read_json())
            if path == "/api/market/sheet/import":
                return self.import_market_sheet(self.current_user(), self.read_json(5_000_000))
            if path == "/api/market/feed/sync":
                return self.sync_market_feed(self.current_user(), self.read_json())
            if path.startswith("/api/market/quotes/") and path.endswith("/delete"):
                return self.delete_market_quote(self.current_user(), path.split("/")[4])
            if path == "/api/market/decisions":
                return self.create_pricing_decision(self.current_user(), self.read_json())
            if path == "/api/import/devices.csv":
                return self.import_devices_csv(self.current_user(), self.read_json(5_000_000))
            if path == "/api/devices/intake": return self.intake(self.current_user(), self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/update"):
                return self.update_device(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/status"):
                return self.change_device_status(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/reserve"):
                return self.reserve_device(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/reservation/cancel"):
                return self.cancel_reservation(self.current_user(), path.split("/")[3])
            if path.startswith("/api/devices/") and path.endswith("/repair/start"):
                return self.start_repair(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/repair/complete"):
                return self.complete_repair(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/return"):
                return self.return_device(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/after-sales"):
                return self.create_after_sales(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/after-sales/") and path.endswith("/resolve"):
                return self.resolve_after_sales(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/devices/") and path.endswith("/photos"):
                return self.add_device_photo(self.current_user(), path.split("/")[3], self.read_json(15_000_000))
            if path == "/api/stocktakes/start": return self.start_stocktake(self.current_user(), self.read_json())
            if path.startswith("/api/stocktakes/") and path.endswith("/scan"):
                return self.scan_stocktake(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/stocktakes/") and path.endswith("/complete"):
                return self.complete_stocktake(self.current_user(), path.split("/")[3])
            if path.startswith("/api/devices/") and path.endswith("/print"):
                return self.print_device(self.current_user(), path.split("/")[3])
            if path.startswith("/api/devices/") and path.endswith("/sell"):
                return self.sell(self.current_user(), path.split("/")[3], self.read_json())
            if path.startswith("/api/sales/") and path.endswith("/update"):
                return self.update_sale(self.current_user(), path.split("/")[3], self.read_json())
            if path == "/api/users": return self.create_user(self.current_user(), self.read_json())
            if path.startswith("/api/users/") and path.endswith("/toggle"):
                return self.toggle_user(self.current_user(), path.split("/")[3])
            raise ApiError(HTTPStatus.NOT_FOUND, "接口不存在")
        except ApiError as error:
            self.send_json({"error": str(error)}, error.status)
        except sqlite3.IntegrityError as error:
            message = "IMEI、库存编号或用户名已经存在" if "UNIQUE" in str(error).upper() else "数据不符合要求"
            self.send_json({"error": message}, HTTPStatus.CONFLICT)
        except Exception as error:
            print(f"API_ERROR {self.path}: {error}", file=sys.stderr)
            LOGGER.exception("POST failed path=%s ip=%s", self.path, self.client_ip)
            self.send_json({"error": "系统处理失败，请查看电脑服务窗口"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def first_setup(self, data: dict) -> None:
        shop_name = str(data.get("shopName", "")).strip()
        username = str(data.get("username", "")).strip()
        display_name = str(data.get("displayName", "")).strip()
        password = str(data.get("password", ""))
        if not shop_name or not username or not display_name or len(password) < 6:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请完整填写，密码至少6位")
        digest, salt = hash_password(password)
        stamp, shop_id, user_id = now_iso(), str(uuid.uuid4()), str(uuid.uuid4())
        with WRITE_LOCK, connect() as db:
            db.execute("begin immediate")
            if db.execute("select exists(select 1 from users)").fetchone()[0]:
                raise ApiError(HTTPStatus.CONFLICT, "系统已经完成首次设置")
            db.execute("insert into shops(id,name,created_at) values(?,?,?)", (shop_id, shop_name, stamp))
            db.execute("insert into users(id,shop_id,username,display_name,role,password_hash,password_salt,created_at) values(?,?,?,?,?,?,?,?)", (user_id, shop_id, username, display_name, "owner", digest, salt, stamp))
            audit_insert(db, shop_id=shop_id, action="first_setup", summary="完成门店首次设置", actor={"id": user_id, "display_name": display_name, "role": "owner"}, entity_type="shop", entity_id=shop_id, client_ip=self.client_ip)
            db.commit()
        self.send_json({"ok": True})

    def login(self, data: dict) -> None:
        username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
        failure_key = f"{self.client_ip}:{username.lower()}"
        current = datetime.now(timezone.utc)
        with LOGIN_LOCK:
            recent = [stamp for stamp in LOGIN_FAILURES.get(failure_key, []) if current - stamp < timedelta(minutes=15)]
            LOGIN_FAILURES[failure_key] = recent
            if len(recent) >= 5:
                raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "登录失败次数过多，请15分钟后再试")
        invalid_login = False
        with connect() as db:
            row = db.execute("select * from users where username=? collate nocase", (username,)).fetchone()
            if not row or not row["active"] or not verify_password(password, row["password_hash"], row["password_salt"]):
                with LOGIN_LOCK:
                    LOGIN_FAILURES.setdefault(failure_key, []).append(current)
                shop_id = row["shop_id"] if row else (db.execute("select id from shops order by created_at limit 1").fetchone() or [None])[0]
                audit_insert(db, shop_id=shop_id, action="login_failed", summary=f"登录失败：{username or '空用户名'}", success=False, client_ip=self.client_ip)
                invalid_login = True
            else:
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
                db.execute("insert into sessions(token_hash,user_id,expires_at,created_at,last_seen_at) values(?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], expires, now_iso(), now_iso()))
                actor = {"id": row["id"], "display_name": row["display_name"], "role": row["role"]}
                audit_insert(db, shop_id=row["shop_id"], action="login", summary="登录系统", actor=actor, entity_type="session", client_ip=self.client_ip)
        if invalid_login:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "用户名或密码不正确")
        with LOGIN_LOCK:
            LOGIN_FAILURES.pop(failure_key, None)
        cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS*86400}"
        self.send_json({"ok": True}, cookie=cookie)

    def logout(self) -> None:
        actor = self.current_user(required=False)
        cookie = SimpleCookie(self.headers.get("Cookie", "")); token = cookie.get(COOKIE_NAME)
        if token:
            with connect() as db:
                db.execute("delete from sessions where token_hash=?", (hashlib.sha256(token.value.encode()).hexdigest(),))
                if actor: audit_insert(db, shop_id=actor["shop_id"], action="logout", summary="退出系统", actor=actor, entity_type="session", client_ip=self.client_ip)
        self.send_json({"ok": True}, cookie=f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def create_user(self, actor: dict, data: dict) -> None:
        if actor["role"] != "owner": raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以添加店员")
        username, display = str(data.get("username", "")).strip(), str(data.get("displayName", "")).strip()
        password = str(data.get("password", "")); role = str(data.get("role", "staff"))
        if not username or not display or len(password) < 6 or role not in ("owner", "staff"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "店员资料不完整")
        digest, salt = hash_password(password)
        user_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("insert into users(id,shop_id,username,display_name,role,password_hash,password_salt,active,created_at) values(?,?,?,?,?,?,?,?,?)", (user_id, actor["shop_id"], username, display, role, digest, salt, 1, now_iso()))
            audit_insert(db, shop_id=actor["shop_id"], action="user_create", summary=f"添加账号：{display}", actor=actor, entity_type="user", entity_id=user_id, details={"username": username, "role": role}, client_ip=self.client_ip)
        self.send_json({"ok": True}, HTTPStatus.CREATED)

    def list_users(self, actor: dict) -> list[dict]:
        if actor["role"] != "owner": raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以查看账号")
        with connect() as db:
            return [dict(row) for row in db.execute("select id,username,display_name,role,active,created_at from users where shop_id=? order by created_at", (actor["shop_id"],))]

    def toggle_user(self,actor:dict,user_id:str)->None:
        if actor["role"]!="owner": raise ApiError(HTTPStatus.FORBIDDEN,"只有老板可以管理员工")
        if actor["id"]==user_id: raise ApiError(HTTPStatus.CONFLICT,"不能停用当前老板账号")
        with connect() as db:
            row=db.execute("select active from users where id=? and shop_id=?",(user_id,actor["shop_id"])).fetchone()
            if not row: raise ApiError(HTTPStatus.NOT_FOUND,"没有找到账号")
            active=0 if row["active"] else 1; db.execute("update users set active=? where id=?",(active,user_id))
            if not active: db.execute("delete from sessions where user_id=?",(user_id,))
            audit_insert(db, shop_id=actor["shop_id"], action="user_toggle", summary=f"{'启用' if active else '停用'}账号", actor=actor, entity_type="user", entity_id=user_id, details={"active": bool(active)}, client_ip=self.client_ip)
        self.send_json({"ok":True,"active":bool(active)})

    def change_password(self, actor: dict, data: dict) -> None:
        old_password, new_password = str(data.get("oldPassword", "")), str(data.get("newPassword", ""))
        if len(new_password) < 6: raise ApiError(HTTPStatus.BAD_REQUEST, "新密码至少6位")
        with connect() as db:
            row = db.execute("select password_hash,password_salt from users where id=?", (actor["id"],)).fetchone()
            if not row or not verify_password(old_password, row["password_hash"], row["password_salt"]):
                raise ApiError(HTTPStatus.UNAUTHORIZED, "原密码不正确")
            digest, salt = hash_password(new_password)
            db.execute("update users set password_hash=?,password_salt=? where id=?", (digest, salt, actor["id"]))
            audit_insert(db, shop_id=actor["shop_id"], action="password_change", summary="修改登录密码", actor=actor, entity_type="user", entity_id=actor["id"], client_ip=self.client_ip)
            db.execute("delete from sessions where user_id=?", (actor["id"],))
        self.send_json({"ok": True}, cookie=f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def list_backups(self, actor: dict) -> list[dict]:
        if actor["role"] != "owner": raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以查看备份")
        backup_dir = DB_PATH.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        return [{"name": path.name, "size": path.stat().st_size, "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")} for path in sorted(backup_dir.glob("store-*.db"), reverse=True)]

    def create_backup(self, actor: dict) -> None:
        if actor["role"] != "owner": raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以备份")
        path = daily_backup(True)
        with connect() as db:
            audit_insert(db, shop_id=actor["shop_id"], action="backup_create", summary=f"手工备份成功：{path.name}", actor=actor, entity_type="backup", entity_id=path.name, client_ip=self.client_ip)
        self.send_json({"ok": True, "name": path.name, "size": path.stat().st_size, "verified": True})

    def restore_backup(self, actor: dict, data: dict) -> None:
        if actor["role"] != "owner": raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以恢复备份")
        name = Path(str(data.get("name", ""))).name
        if data.get("confirmation") != "RESTORE" or not name.startswith("store-") or not name.endswith(".db"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "恢复确认信息不正确")
        backup = DB_PATH.parent / "backups" / name
        if not backup.exists(): raise ApiError(HTTPStatus.NOT_FOUND, "备份文件不存在")
        safety = daily_backup(True)
        with WRITE_LOCK:
            source, destination = sqlite3.connect(backup), connect()
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        migrate_db()
        database_check()
        with connect() as db:
            audit_insert(db, shop_id=actor["shop_id"], action="backup_restore", summary=f"恢复备份：{name}", actor=actor, entity_type="backup", entity_id=name, details={"safetyBackup": safety.name}, client_ip=self.client_ip)
        self.send_json({"ok": True, "restored": name, "safetyBackup": safety.name})

    def dashboard(self, user: dict) -> dict:
        with connect() as db:
            active = db.execute("select count(*) from devices where shop_id=? and deleted_at is null and status in ('in_stock','reserved','sold_pending_pickup')", (user["shop_id"],)).fetchone()[0]
            today_intake = db.execute("select count(*) from devices where shop_id=? and deleted_at is null and date(created_at,'localtime')=date('now','localtime')", (user["shop_id"],)).fetchone()[0]
            reserved = db.execute("select count(*) from devices where shop_id=? and deleted_at is null and status='reserved'", (user["shop_id"],)).fetchone()[0]
            pending_pickup = db.execute("select count(*) from devices where shop_id=? and deleted_at is null and status='sold_pending_pickup'", (user["shop_id"],)).fetchone()[0]
            unprinted = db.execute("select count(*) from devices d where d.shop_id=? and d.deleted_at is null and d.status in ('in_stock','reserved','sold_pending_pickup') and not exists(select 1 from print_jobs p where p.device_id=d.id and p.status='printed')", (user["shop_id"],)).fetchone()[0]
            aged = db.execute("select count(*) from devices where shop_id=? and deleted_at is null and status in ('in_stock','reserved') and julianday('now')-julianday(created_at)>30", (user["shop_id"],)).fetchone()[0]
            sold = db.execute("select count(*),coalesce(sum(sale_price),0) from sales where shop_id=? and date(sold_at,'localtime')=date('now','localtime')", (user["shop_id"],)).fetchone()
            returned = db.execute("select count(*),coalesce(sum(refund_amount),0) from returns where shop_id=? and date(created_at,'localtime')=date('now','localtime')", (user["shop_id"],)).fetchone()
            result = {"activeCount": active, "todayIntake": today_intake, "reservedCount": reserved, "pendingPickupCount": pending_pickup, "unprintedCount": unprinted, "agedCount": aged, "todaySold": max(0,sold[0]-returned[0]), "todayRevenue": sold[1]-returned[1], "role": user["role"]}
            if user["role"] == "owner":
                result["inventoryCost"] = db.execute("select coalesce(sum(f.purchase_cost),0) from devices d join device_financials f on f.device_id=d.id where d.shop_id=? and d.deleted_at is null and d.status in ('in_stock','reserved','sold_pending_pickup')", (user["shop_id"],)).fetchone()[0]
                gross = db.execute("select coalesce(sum(s.sale_price-s.purchase_cost_snapshot),0) from sales s where s.shop_id=? and date(s.sold_at,'localtime')=date('now','localtime')", (user["shop_id"],)).fetchone()[0]
                return_impact = db.execute("""select coalesce(sum(case when r.disposition='restock' then r.refund_amount-s.purchase_cost_snapshot else r.refund_amount end),0) from returns r left join sales s on s.id=r.sale_id where r.shop_id=? and date(r.created_at,'localtime')=date('now','localtime')""",(user["shop_id"],)).fetchone()[0]
                result["todayProfit"] = gross-return_impact
            return result

    def operations_alerts(self, user: dict) -> dict:
        with connect() as db:
            aged_rows = db.execute("""select
                sum(case when julianday('now','localtime')-julianday(d.created_at,'localtime') between 31 and 59.999 then 1 else 0 end) aged_30,
                sum(case when julianday('now','localtime')-julianday(d.created_at,'localtime') between 60 and 89.999 then 1 else 0 end) aged_60,
                sum(case when julianday('now','localtime')-julianday(d.created_at,'localtime') >= 90 then 1 else 0 end) aged_90,
                coalesce(sum(case when julianday('now','localtime')-julianday(d.created_at,'localtime') >= 30 then f.purchase_cost else 0 end),0) aged_capital
                from devices d left join device_financials f on f.device_id=d.id
                where d.shop_id=? and d.deleted_at is null and d.status in ('in_stock','reserved')""", (user["shop_id"],)).fetchone()
            expired = db.execute("""select count(*) from reservations r join devices d on d.id=r.device_id
                where r.shop_id=? and r.status='active' and r.expires_at is not null and trim(r.expires_at)<>''
                and datetime(r.expires_at)<datetime('now','localtime') and d.status='reserved'""", (user["shop_id"],)).fetchone()[0]
            pending = db.execute("select count(*) from devices where shop_id=? and deleted_at is null and status='sold_pending_pickup' and julianday('now')-julianday(updated_at)>3", (user["shop_id"],)).fetchone()[0]
            repairs = db.execute("""select count(*) from repairs r join devices d on d.id=r.device_id where r.shop_id=?
                and r.status in ('sent','repairing') and d.status='in_repair' and julianday('now')-julianday(r.sent_at)>7""", (user["shop_id"],)).fetchone()[0]
            after_sales = db.execute("select count(*) from after_sales_cases where shop_id=? and status='open'", (user["shop_id"],)).fetchone()[0]
        items = []
        for count, severity, title, message, scope in (
            (aged_rows["aged_90"] or 0, "danger", "90天以上库存", "优先复检成色和价格，决定降价、同行调拨或止损。", "aged90"),
            (aged_rows["aged_60"] or 0, "warning", "60–89天库存", "本周逐台检查标价和客户询价记录。", "aged60"),
            (aged_rows["aged_30"] or 0, "notice", "31–59天库存", "开始重点曝光，避免继续占用资金。", "aged"),
            (expired, "warning", "预订已到期", "联系客户确认，或取消预订恢复销售。", "reserved"),
            (pending, "warning", "已售待取超过3天", "联系客户安排取机，避免账物状态不一致。", "pending_pickup"),
            (repairs, "warning", "送修超过7天", "向维修方追踪进度并更新记录。", "in_repair"),
            (after_sales, "danger", "待处理售后", "尽快联系客户并记录处理结果。", "sold"),
        ):
            if count:
                items.append({"severity": severity, "title": title, "count": int(count), "message": message, "scope": scope})
        backup = backup_state()
        if not backup.get("verified"):
            items.insert(0, {"severity": "danger", "title": "备份需要检查", "count": 1, "message": "请在设置中立即备份，确认显示校验通过。", "scope": ""})
        result = {"items": items, "count": sum(int(item["count"]) for item in items), "generatedAt": now_iso()}
        if user["role"] == "owner":
            result["agedCapital"] = float(aged_rows["aged_capital"] or 0)
        return result

    def recognize_screenshot(self, user: dict, data: dict) -> None:
        if recognize_device_screenshot is None:
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "本地截图识别组件没有安装")
        encoded = str(data.get("image", ""))
        match = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", encoded, re.I | re.S)
        if not match:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请选择PNG、JPG或WebP截图")
        try:
            image = base64.b64decode(match.group(2), validate=True)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "截图数据不正确")
        if not 1_000 <= len(image) <= 10_000_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "截图大小不正确")
        suffix = ".jpg" if match.group(1).lower() in ("jpeg", "jpg") else f".{match.group(1).lower()}"
        screenshot_dir = DB_PATH.parent / "source-screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        image_path = screenshot_dir / f"{uuid.uuid4()}{suffix}"
        image_path.write_bytes(image)
        try:
            result = recognize_device_screenshot(image_path)
        except ValueError as error:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(error))
        except Exception:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "本地识别失败，请重新上传清晰截图")
        result.setdefault("sourceFields", {})["screenshotRef"] = str(image_path.relative_to(DB_PATH.parent))
        result["recognizedBy"] = "local-ocr"
        self.send_json(result)

    def recognize_qr(self, user: dict, data: dict) -> None:
        encoded = str(data.get("image", ""))
        match = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", encoded, re.I | re.S)
        if not match: raise ApiError(HTTPStatus.BAD_REQUEST, "请选择二维码照片")
        try:
            image_bytes = base64.b64decode(match.group(2), validate=True)
            value = decode_qr_image(image_bytes)
        except Exception:
            value = ""
        value = value.strip()
        if not value: raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "没有识别到二维码，请靠近标签重新拍摄")
        self.send_json({"value": value})

    def list_devices(self, user: dict) -> list[dict]:
        query = parse_qs(urlparse(self.path).query); text = query.get("q", [""])[0].strip(); status = query.get("status", [""])[0]; scope = query.get("scope", [""])[0]
        print_status_sql = "case when exists(select 1 from print_jobs p where p.device_id=d.id and p.status='printed') then 'printed' else (select p.status from print_jobs p where p.device_id=d.id order by p.requested_at desc limit 1) end print_status"
        sql = "select d.*," + print_status_sql + (",f.purchase_cost,f.minimum_price" if user["role"] == "owner" else "") + " from devices d" + (" left join device_financials f on f.device_id=d.id" if user["role"] == "owner" else "") + " where d.shop_id=? and d.deleted_at is null"
        args = [user["shop_id"]]
        if status: sql += " and d.status=?"; args.append(status)
        elif scope == "today_intake": sql += " and date(d.created_at,'localtime')=date('now','localtime')"
        elif scope == "today_sold": sql += " and d.status='sold' and exists(select 1 from sales s where s.device_id=d.id and date(s.sold_at,'localtime')=date('now','localtime'))"
        elif scope == "reserved": sql += " and d.status='reserved'"
        elif scope == "pending_pickup": sql += " and d.status='sold_pending_pickup'"
        elif scope == "in_stock": sql += " and d.status='in_stock'"
        elif scope == "unprinted": sql += " and d.status in ('in_stock','reserved','sold_pending_pickup') and not exists(select 1 from print_jobs p where p.device_id=d.id and p.status='printed')"
        elif scope == "aged": sql += " and d.status in ('in_stock','reserved') and julianday('now')-julianday(d.created_at)>30"
        elif scope == "aged60": sql += " and d.status in ('in_stock','reserved') and julianday('now')-julianday(d.created_at)>=60 and julianday('now')-julianday(d.created_at)<90"
        elif scope == "aged90": sql += " and d.status in ('in_stock','reserved') and julianday('now')-julianday(d.created_at)>=90"
        elif scope == "in_repair": sql += " and d.status='in_repair'"
        if text: sql += " and (d.stock_code like ? or d.model like ? or d.imei like ? or d.serial_number like ?)"; args.extend([f"%{text}%"]*4)
        sql += " order by d.created_at desc limit 500"
        with connect() as db: rows = [dict(row) for row in db.execute(sql, args)]
        for row in rows:
            if row.get("imei"):
                row["imei_tail"] = row["imei"][-4:]
                row["imei_masked"] = row["imei"][:3] + "•••••••••" + row["imei"][-3:]
            if user["role"] != "owner": row.pop("imei", None); row.pop("imei2", None)
        return rows

    def device_detail(self, user: dict, device_id: str) -> dict:
        sql = "select d.*" + (",f.purchase_cost,f.minimum_price" if user["role"] == "owner" else "") + " from devices d" + (" left join device_financials f on f.device_id=d.id" if user["role"] == "owner" else "") + " where d.id=? and d.shop_id=? and d.deleted_at is null"
        with connect() as db:
            row = db.execute(sql, (device_id, user["shop_id"])).fetchone()
            if not row: raise ApiError(HTTPStatus.NOT_FOUND, "没有找到设备")
            result = dict(row)
            result["print_status"] = "printed" if db.execute("select exists(select 1 from print_jobs where device_id=? and status='printed')", (device_id,)).fetchone()[0] else None
            result["events"] = [dict(item) for item in db.execute("select e.*,u.display_name actor_name from inventory_events e join users u on u.id=e.actor_id where e.device_id=? order by e.created_at desc,e.id desc limit 100", (device_id,))]
            result["photos"] = [dict(item) for item in db.execute("select id,photo_type,description,created_at from device_photos where device_id=? order by created_at", (device_id,))]
            reservation = db.execute("select * from reservations where device_id=? and status='active' order by created_at desc limit 1", (device_id,)).fetchone()
            repair = db.execute("select * from repairs where device_id=? and status in ('sent','repairing') order by sent_at desc limit 1", (device_id,)).fetchone()
            sale = db.execute("select * from sales where device_id=? order by sold_at desc limit 1", (device_id,)).fetchone()
            cases = [dict(item) for item in db.execute("""select a.*,u.display_name created_by_name,coalesce(c.display_name,'') closed_by_name
                from after_sales_cases a join users u on u.id=a.created_by left join users c on c.id=a.closed_by
                where a.device_id=? order by a.created_at desc""", (device_id,))]
            latest_sale = dict(sale) if sale else None
            if latest_sale:
                expires = latest_sale.get("warranty_expires_at")
                if not expires and latest_sale.get("warranty_days"):
                    expires = (datetime.fromisoformat(latest_sale["sold_at"]) + timedelta(days=int(latest_sale["warranty_days"]))).isoformat(timespec="seconds")
                    latest_sale["warranty_expires_at"] = expires
                latest_sale["warranty_status"] = "none" if not expires else ("active" if datetime.fromisoformat(expires) >= datetime.now(timezone.utc) else "expired")
            result["reservation"] = dict(reservation) if reservation else None; result["repair"] = dict(repair) if repair else None; result["latestSale"] = latest_sale; result["afterSales"] = cases
        if user["role"] != "owner":
            result.pop("imei", None); result.pop("imei2", None)
            if result.get("latestSale"):
                result["latestSale"].pop("purchase_cost_snapshot", None)
                serial = result["latestSale"].get("imei_snapshot", "")
                result["latestSale"]["imei_snapshot"] = ("••••" + serial[-4:]) if serial else ""
            for case in result.get("afterSales", []):
                case.pop("service_cost", None)
        return result

    def update_device(self, user: dict, device_id: str, data: dict) -> None:
        allowed = {"model":"model","storage":"storage","color":"color","systemVersion":"system_version","batteryHealth":"battery_health","chargeCycles":"charge_cycles","conditionGrade":"condition_grade","listPrice":"list_price","area":"area","notes":"notes"}
        values = {column: data[key] for key, column in allowed.items() if key in data}
        if not values: raise ApiError(HTTPStatus.BAD_REQUEST, "没有需要修改的内容")
        if "list_price" in values and float(values["list_price"]) < 0: raise ApiError(HTTPStatus.BAD_REQUEST, "售价不能小于0")
        stamp = now_iso()
        with WRITE_LOCK, connect() as db:
            before = db.execute("select * from devices where id=? and shop_id=? and deleted_at is null", (device_id,user["shop_id"])).fetchone()
            if not before: raise ApiError(HTTPStatus.NOT_FOUND, "没有找到设备")
            changes = {column:{"before":before[column],"after":value} for column,value in values.items() if str(before[column] if before[column] is not None else "") != str(value if value is not None else "")}
            if not changes: return self.send_json({"ok": True, "changed": 0})
            assignments = ",".join(f"{column}=?" for column in values)
            db.execute(f"update devices set {assignments},updated_by=?,updated_at=? where id=?", (*values.values(),user["id"],stamp,device_id))
            if user["role"] == "owner" and "purchaseCost" in data:
                db.execute("update device_financials set purchase_cost=?,updated_by=?,updated_at=? where device_id=?", (float(data["purchaseCost"]),user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,metadata,actor_id,created_at) values(?,?,?,?,?,?,?,?,?)", (user["shop_id"],device_id,"edit",before["status"],before["status"],"修改设备资料",json.dumps(changes,ensure_ascii=False),user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="device_update", summary=f"修改设备：{before['stock_code']}", actor=user, entity_type="device", entity_id=device_id, details={"fields": list(changes)}, client_ip=self.client_ip)
        self.send_json({"ok": True, "changed": len(changes)})

    def change_device_status(self, user: dict, device_id: str, data: dict) -> None:
        target = str(data.get("status", ""))
        allowed = {"in_stock","reserved","sold_pending_pickup","in_repair","borrowed_for_test","peer_transfer","return_processing","scrapped"}
        if target not in allowed: raise ApiError(HTTPStatus.BAD_REQUEST, "目标状态不正确")
        stamp = now_iso()
        with WRITE_LOCK, connect() as db:
            device = db.execute("select * from devices where id=? and shop_id=? and deleted_at is null", (device_id,user["shop_id"])).fetchone()
            if not device: raise ApiError(HTTPStatus.NOT_FOUND, "没有找到设备")
            if device["status"] == "sold": raise ApiError(HTTPStatus.CONFLICT, "已售设备请通过退货流程改变状态")
            db.execute("update devices set status=?,updated_by=?,updated_at=? where id=?", (target,user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?,?)", (user["shop_id"],device_id,"status_change",device["status"],target,str(data.get("note", "")),user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="device_status", summary=f"{device['stock_code']}：{device['status']} → {target}", actor=user, entity_type="device", entity_id=device_id, client_ip=self.client_ip)
        self.send_json({"ok": True, "status": target})

    def reserve_device(self, user: dict, device_id: str, data: dict) -> None:
        customer = str(data.get("customerName", "")).strip()
        if not customer: raise ApiError(HTTPStatus.BAD_REQUEST, "请填写客户姓名或称呼")
        deposit = float(data.get("deposit") or 0); stamp = now_iso()
        with WRITE_LOCK, connect() as db:
            device = db.execute("select * from devices where id=? and shop_id=?", (device_id,user["shop_id"])).fetchone()
            if not device or device["status"] != "in_stock": raise ApiError(HTTPStatus.CONFLICT, "只有在库设备可以预订")
            db.execute("insert into reservations values(?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),user["shop_id"],device_id,customer,str(data.get("customerPhone", "")),deposit,data.get("expiresAt") or None,"active",str(data.get("note", "")),user["id"],stamp,stamp))
            db.execute("update devices set status='reserved',updated_by=?,updated_at=? where id=?", (user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,metadata,actor_id,created_at) values(?,?,?,?,?,?,?,?,?)", (user["shop_id"],device_id,"reserve","in_stock","reserved",customer,json.dumps({"deposit":deposit,"expiresAt":data.get("expiresAt")},ensure_ascii=False),user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="reservation_create", summary=f"预订设备：{device['stock_code']}", actor=user, entity_type="device", entity_id=device_id, details={"deposit": deposit}, client_ip=self.client_ip)
        self.send_json({"ok": True})

    def cancel_reservation(self, user: dict, device_id: str) -> None:
        stamp=now_iso()
        with WRITE_LOCK, connect() as db:
            row=db.execute("select id from reservations where device_id=? and shop_id=? and status='active' order by created_at desc limit 1",(device_id,user["shop_id"])).fetchone()
            if not row: raise ApiError(HTTPStatus.NOT_FOUND,"没有有效预订")
            db.execute("update reservations set status='cancelled',updated_at=? where id=?",(stamp,row["id"]))
            db.execute("update devices set status='in_stock',updated_by=?,updated_at=? where id=?",(user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?,?)",(user["shop_id"],device_id,"reservation_cancel","reserved","in_stock","取消预订",user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="reservation_cancel", summary="取消设备预订", actor=user, entity_type="device", entity_id=device_id, client_ip=self.client_ip)
        self.send_json({"ok":True})

    def start_repair(self,user:dict,device_id:str,data:dict)->None:
        issue=str(data.get("issue","")).strip()
        if not issue: raise ApiError(HTTPStatus.BAD_REQUEST,"请填写送修问题")
        stamp=now_iso()
        with WRITE_LOCK,connect() as db:
            device=db.execute("select * from devices where id=? and shop_id=?",(device_id,user["shop_id"])).fetchone()
            if not device or device["status"]=="sold": raise ApiError(HTTPStatus.CONFLICT,"当前设备不能送修")
            db.execute("insert into repairs values(?,?,?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),user["shop_id"],device_id,str(data.get("vendor","")),issue,float(data.get("cost") or 0),"sent",stamp,None,user["id"],stamp))
            db.execute("update devices set status='in_repair',updated_by=?,updated_at=? where id=?",(user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?,?)",(user["shop_id"],device_id,"repair_start",device["status"],"in_repair",issue,user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="repair_start", summary=f"设备送修：{device['stock_code']}", actor=user, entity_type="device", entity_id=device_id, client_ip=self.client_ip)
        self.send_json({"ok":True})

    def complete_repair(self,user:dict,device_id:str,data:dict)->None:
        stamp=now_iso(); target=str(data.get("status","in_stock"))
        if target not in ("in_stock","scrapped"): raise ApiError(HTTPStatus.BAD_REQUEST,"维修完成状态不正确")
        with WRITE_LOCK,connect() as db:
            repair=db.execute("select id from repairs where device_id=? and shop_id=? and status in ('sent','repairing') order by sent_at desc limit 1",(device_id,user["shop_id"])).fetchone()
            if not repair: raise ApiError(HTTPStatus.NOT_FOUND,"没有进行中的维修")
            db.execute("update repairs set status='completed',returned_at=?,updated_at=?,cost=? where id=?",(stamp,stamp,float(data.get("cost") or 0),repair["id"]))
            db.execute("update devices set status=?,updated_by=?,updated_at=? where id=?",(target,user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?,?)",(user["shop_id"],device_id,"repair_complete","in_repair",target,str(data.get("note","")),user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="repair_complete", summary=f"维修完成，状态：{target}", actor=user, entity_type="device", entity_id=device_id, details={"cost": float(data.get("cost") or 0)}, client_ip=self.client_ip)
        self.send_json({"ok":True})

    def return_device(self,user:dict,device_id:str,data:dict)->None:
        reason=str(data.get("reason","")).strip(); disposition=str(data.get("disposition","restock"))
        if not reason or disposition not in ("restock","repair","scrap"): raise ApiError(HTTPStatus.BAD_REQUEST,"退货资料不完整")
        target={"restock":"in_stock","repair":"in_repair","scrap":"scrapped"}[disposition]; stamp=now_iso()
        with WRITE_LOCK,connect() as db:
            device=db.execute("select * from devices where id=? and shop_id=?",(device_id,user["shop_id"])).fetchone(); sale=db.execute("select * from sales where device_id=? order by sold_at desc limit 1",(device_id,)).fetchone()
            if not device or device["status"]!="sold" or not sale: raise ApiError(HTTPStatus.CONFLICT,"只有已售设备可以退货")
            db.execute("insert into returns values(?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),user["shop_id"],device_id,sale["id"],float(data.get("refundAmount") or sale["sale_price"]),reason,disposition,user["id"],stamp))
            db.execute("update devices set status=?,updated_by=?,updated_at=? where id=?",(target,user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,metadata,actor_id,created_at) values(?,?,?,?,?,?,?,?,?)",(user["shop_id"],device_id,"return","sold",target,reason,json.dumps({"refundAmount":data.get("refundAmount") or sale["sale_price"]},ensure_ascii=False),user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="sale_return", summary=f"销售退货：{device['stock_code']}", actor=user, entity_type="sale", entity_id=sale["id"], details={"refundAmount": float(data.get("refundAmount") or sale["sale_price"]), "disposition": disposition}, client_ip=self.client_ip)
        self.send_json({"ok":True,"status":target})

    def create_after_sales(self, user: dict, device_id: str, data: dict) -> None:
        issue = str(data.get("issue", "")).strip()
        if not issue:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写客户反馈的问题")
        stamp, case_id = now_iso(), str(uuid.uuid4())
        with WRITE_LOCK, connect() as db:
            device = db.execute("select * from devices where id=? and shop_id=? and deleted_at is null", (device_id, user["shop_id"])).fetchone()
            sale = db.execute("select * from sales where device_id=? and shop_id=? order by sold_at desc limit 1", (device_id, user["shop_id"])).fetchone()
            if not device or not sale:
                raise ApiError(HTTPStatus.CONFLICT, "该设备没有销售记录，不能登记售后")
            db.execute("""insert into after_sales_cases(id,shop_id,device_id,sale_id,issue,created_by,created_at,updated_at)
                values(?,?,?,?,?,?,?,?)""", (case_id, user["shop_id"], device_id, sale["id"], issue, user["id"], stamp, stamp))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?,?)", (user["shop_id"], device_id, "after_sales_open", device["status"], device["status"], issue, user["id"], stamp))
            audit_insert(db, shop_id=user["shop_id"], action="after_sales_open", summary=f"登记售后：{device['stock_code']}", actor=user, entity_type="after_sales", entity_id=case_id, client_ip=self.client_ip)
        self.send_json({"ok": True, "id": case_id}, HTTPStatus.CREATED)

    def resolve_after_sales(self, user: dict, case_id: str, data: dict) -> None:
        resolution = str(data.get("resolution", "")).strip()
        if not resolution:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写售后处理结果")
        try:
            cost = float(data.get("serviceCost") or 0)
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "售后成本格式不正确")
        if cost < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "售后成本不能小于0")
        stamp = now_iso()
        with WRITE_LOCK, connect() as db:
            case = db.execute("""select a.*,d.stock_code,d.status device_status from after_sales_cases a join devices d on d.id=a.device_id
                where a.id=? and a.shop_id=?""", (case_id, user["shop_id"])).fetchone()
            if not case:
                raise ApiError(HTTPStatus.NOT_FOUND, "没有找到售后记录")
            if case["status"] != "open":
                raise ApiError(HTTPStatus.CONFLICT, "这条售后已经处理完成")
            db.execute("update after_sales_cases set status='resolved',resolution=?,service_cost=?,closed_by=?,closed_at=?,updated_at=? where id=?", (resolution, cost, user["id"], stamp, stamp, case_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,metadata,actor_id,created_at) values(?,?,?,?,?,?,?,?,?)", (user["shop_id"], case["device_id"], "after_sales_resolved", case["device_status"], case["device_status"], resolution, json.dumps({"serviceCost": cost}, ensure_ascii=False), user["id"], stamp))
            audit_insert(db, shop_id=user["shop_id"], action="after_sales_resolve", summary=f"完成售后：{case['stock_code']}", actor=user, entity_type="after_sales", entity_id=case_id, details={"serviceCost": cost}, client_ip=self.client_ip)
        self.send_json({"ok": True})

    def current_stocktake(self,user:dict)->dict:
        with connect() as db:
            take=db.execute("select * from stocktakes where shop_id=? and status='open' order by started_at desc limit 1",(user["shop_id"],)).fetchone()
            if not take:return {"open":False}
            area=take["area"]; area_sql=" and area=?" if area else ""; args=[user["shop_id"]]+([area] if area else [])
            expected=db.execute("select count(*) from devices where shop_id=? and deleted_at is null and status in ('in_stock','reserved','sold_pending_pickup')"+area_sql,args).fetchone()[0]
            scanned=db.execute("select count(*) from stocktake_items where stocktake_id=?",(take["id"],)).fetchone()[0]
            missing=[dict(row) for row in db.execute("select id,stock_code,model,storage,area from devices where shop_id=? and deleted_at is null and status in ('in_stock','reserved','sold_pending_pickup')"+area_sql+" and id not in(select device_id from stocktake_items where stocktake_id=?)",args+[take["id"]])]
        return {"open":True,"id":take["id"],"area":area,"expected":expected,"scanned":scanned,"missing":missing}

    def start_stocktake(self,user:dict,data:dict)->None:
        stamp=now_iso(); take_id=str(uuid.uuid4())
        with WRITE_LOCK,connect() as db:
            if db.execute("select exists(select 1 from stocktakes where shop_id=? and status='open')",(user["shop_id"],)).fetchone()[0]: raise ApiError(HTTPStatus.CONFLICT,"已有进行中的盘点")
            db.execute("insert into stocktakes values(?,?,?,?,?,?,?)",(take_id,user["shop_id"],str(data.get("area","")),"open",user["id"],stamp,None))
        self.send_json({"ok":True,"id":take_id},HTTPStatus.CREATED)

    def scan_stocktake(self,user:dict,take_id:str,data:dict)->None:
        code=str(data.get("code","")).strip(); stamp=now_iso()
        with WRITE_LOCK,connect() as db:
            take=db.execute("select * from stocktakes where id=? and shop_id=? and status='open'",(take_id,user["shop_id"])).fetchone()
            if not take: raise ApiError(HTTPStatus.NOT_FOUND,"没有进行中的盘点")
            device=db.execute("select * from devices where shop_id=? and deleted_at is null and (stock_code=? or imei=? or substr(imei,-4)=?)",(user["shop_id"],code,code,code)).fetchone()
            if not device: raise ApiError(HTTPStatus.NOT_FOUND,"未入库设备")
            if take["area"] and device["area"]!=take["area"]: raise ApiError(HTTPStatus.CONFLICT,f"设备属于{device['area']}，不在本次盘点区域")
            try: db.execute("insert into stocktake_items values(?,?,?,?)",(take_id,device["id"],user["id"],stamp)); duplicate=False
            except sqlite3.IntegrityError: duplicate=True
        self.send_json({"ok":True,"duplicate":duplicate,"device":{"id":device["id"],"stockCode":device["stock_code"],"model":device["model"]}})

    def complete_stocktake(self,user:dict,take_id:str)->None:
        stamp=now_iso()
        with WRITE_LOCK,connect() as db:
            take=db.execute("select id from stocktakes where id=? and shop_id=? and status='open'",(take_id,user["shop_id"])).fetchone()
            if not take: raise ApiError(HTTPStatus.NOT_FOUND,"盘点不存在")
            db.execute("update stocktakes set status='completed',completed_at=? where id=?",(stamp,take_id))
        self.send_json({"ok":True})

    def next_stock_code(self, db, shop_id: str, brand: str) -> str:
        prefix = "A" if brand.lower() == "apple" else "H" if brand.startswith("华为") else "M"
        day = datetime.now().strftime("%Y-%m-%d")
        db.execute("insert or ignore into counters(shop_id,counter_date,prefix,last_value) values(?,?,?,0)", (shop_id, day, prefix))
        db.execute("update counters set last_value=last_value+1 where shop_id=? and counter_date=? and prefix=?", (shop_id, day, prefix))
        value = db.execute("select last_value from counters where shop_id=? and counter_date=? and prefix=?", (shop_id, day, prefix)).fetchone()[0]
        return f"{prefix}{datetime.now().strftime('%y%m%d')}-{value:03d}"

    def intake(self, user: dict, data: dict) -> None:
        required = ("brand","model","storage","imei","purchaseCost","listPrice")
        if any(str(data.get(key, "")).strip() == "" for key in required): raise ApiError(HTTPStatus.BAD_REQUEST, "入库资料不完整")
        imei = re.sub(r"\D", "", str(data.get("imei", "")))
        imei2 = re.sub(r"\D", "", str(data.get("imei2", "")))
        if len(imei) != 15: raise ApiError(HTTPStatus.BAD_REQUEST, "IMEI必须是15位数字")
        if imei2 and len(imei2) != 15: raise ApiError(HTTPStatus.BAD_REQUEST, "IMEI2必须是15位数字")
        try:
            purchase_cost, list_price = float(data["purchaseCost"]), float(data["listPrice"])
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "成本或售价格式不正确")
        if purchase_cost < 0 or list_price < 0: raise ApiError(HTTPStatus.BAD_REQUEST, "成本和售价不能小于0")
        if list_price < purchase_cost and not data.get("allowBelowCost"):
            raise ApiError(HTTPStatus.CONFLICT, "销售标价低于收货成本，如确认仍要入库请再次确认")
        stamp, device_id = now_iso(), str(uuid.uuid4())
        with WRITE_LOCK, connect() as db:
            db.execute("begin immediate"); code = self.next_stock_code(db, user["shop_id"], str(data["brand"]))
            db.execute("""insert into devices(id,shop_id,stock_code,brand,model,storage,color,system_version,battery_health,charge_cycles,condition_grade,list_price,imei,imei2,serial_number,area,notes,source_fields,created_by,updated_by,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (device_id,user["shop_id"],code,data["brand"],data["model"],data["storage"],data.get("color", ""),data.get("systemVersion", ""),data.get("batteryHealth") or None,data.get("chargeCycles") or None,data.get("conditionGrade", ""),list_price,imei,imei2 or None,data.get("serialNumber") or None,data.get("area", "默认区"),data.get("notes", ""),json.dumps(data.get("sourceFields", {}),ensure_ascii=False),user["id"],user["id"],stamp,stamp))
            db.execute("insert into device_financials values(?,?,?,?,?,?)", (device_id,user["shop_id"],purchase_cost,float(data["minimumPrice"]) if data.get("minimumPrice") not in (None, "") else None,user["id"],stamp))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,to_status,actor_id,created_at) values(?,?,?,?,?,?)", (user["shop_id"],device_id,"intake","in_stock",user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="device_intake", summary=f"入库：{code} {data['model']} {data['storage']}", actor=user, entity_type="device", entity_id=device_id, details={"stockCode": code}, client_ip=self.client_ip)
            screenshot_ref = data.get("sourceFields", {}).get("screenshotRef") if isinstance(data.get("sourceFields"), dict) else None
            if screenshot_ref:
                db.execute("insert into device_photos(id,shop_id,device_id,photo_type,file_path,description,created_by,created_at) values(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),user["shop_id"],device_id,"inspection_screenshot",str(screenshot_ref),"入库验机截图",user["id"],stamp))
            payload={"model":data["model"],"system":data.get("systemVersion",""),"storage":data["storage"],"battery":data.get("batteryHealth"),"serial":data["imei"],"price":data["listPrice"],"stock_code":code}
            db.execute("insert into print_jobs(id,shop_id,device_id,payload,requested_by,requested_at) values(?,?,?,?,?,?)", (str(uuid.uuid4()),user["shop_id"],device_id,json.dumps(payload,ensure_ascii=False),user["id"],stamp))
            db.commit()
        self.send_json({"ok": True, "id": device_id, "stockCode": code}, HTTPStatus.CREATED)

    def sell(self, user: dict, device_id: str, data: dict) -> None:
        try: price = float(data.get("salePrice"))
        except (TypeError, ValueError): raise ApiError(HTTPStatus.BAD_REQUEST, "成交价不正确")
        try: warranty_days = int(data.get("warrantyDays", 30) or 0)
        except (TypeError, ValueError): raise ApiError(HTTPStatus.BAD_REQUEST, "质保天数不正确")
        if warranty_days < 0 or warranty_days > 3650: raise ApiError(HTTPStatus.BAD_REQUEST, "质保天数应在0到3650天之间")
        stamp = now_iso()
        warranty_expires = (datetime.fromisoformat(stamp) + timedelta(days=warranty_days)).isoformat(timespec="seconds") if warranty_days else None
        with WRITE_LOCK, connect() as db:
            db.execute("begin immediate")
            device = db.execute("select d.*,coalesce(f.purchase_cost,0) purchase_cost from devices d left join device_financials f on f.device_id=d.id where d.id=? and d.shop_id=? and d.deleted_at is null", (device_id,user["shop_id"])).fetchone()
            if not device: raise ApiError(HTTPStatus.NOT_FOUND, "没有找到设备")
            if device["status"] not in ("in_stock","reserved","sold_pending_pickup"): raise ApiError(HTTPStatus.CONFLICT, "设备当前状态不能出库")
            sale_id = str(uuid.uuid4())
            truthy = lambda value: 1 if str(value).lower() in ("1", "true", "on", "yes") else 0
            db.execute("""insert into sales(id,shop_id,device_id,sale_price,payment_method,customer_note,sold_by,sold_at,model_snapshot,storage_snapshot,imei_snapshot,purchase_cost_snapshot,gift_case,gift_screen_protector,gift_charging_head,gift_charger,warranty_days,warranty_expires_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (sale_id,user["shop_id"],device_id,price,str(data.get("paymentMethod", "")),str(data.get("customerNote", "")),user["id"],stamp,device["model"],device["storage"],device["imei"] or device["serial_number"] or "",device["purchase_cost"],truthy(data.get("giftCase")),truthy(data.get("giftScreenProtector")),truthy(data.get("giftChargingHead")),truthy(data.get("giftCharger")),warranty_days,warranty_expires))
            db.execute("update devices set status='sold',updated_by=?,updated_at=? where id=?", (user["id"],stamp,device_id))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?,?)", (user["shop_id"],device_id,"sale",device["status"],"sold",str(data.get("customerNote", "")),user["id"],stamp))
            audit_insert(db, shop_id=user["shop_id"], action="device_sale", summary=f"出库：{device['stock_code']}，成交价{price:g}元", actor=user, entity_type="sale", entity_id=sale_id, details={"stockCode": device["stock_code"], "warrantyDays": warranty_days}, client_ip=self.client_ip)
            db.commit()
        self.send_json({"ok": True, "saleId": sale_id})

    def daily_ledger(self, user: dict) -> dict:
        query = parse_qs(urlparse(self.path).query)
        day = query.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ApiError(HTTPStatus.BAD_REQUEST, "日期格式不正确")
        with connect() as db:
            rows = [dict(row) for row in db.execute("""select s.id,s.sold_at,s.sale_price,s.payment_method,s.customer_note,
                s.model_snapshot model,s.storage_snapshot storage,s.imei_snapshot imei,s.purchase_cost_snapshot,
                s.gift_case,s.gift_screen_protector,s.gift_charging_head,s.gift_charger,
                u.display_name sold_by_name,d.stock_code,
                case when exists(select 1 from returns r where r.sale_id=s.id) then 1 else 0 end returned
                from sales s join users u on u.id=s.sold_by join devices d on d.id=s.device_id
                where s.shop_id=? and date(s.sold_at,'localtime')=date(?) order by s.sold_at,s.id""", (user["shop_id"], day))]
        summary = {
            "count": len(rows),
            "revenue": sum(float(row["sale_price"]) for row in rows),
            "giftCase": sum(row["gift_case"] for row in rows),
            "giftScreenProtector": sum(row["gift_screen_protector"] for row in rows),
            "giftChargingHead": sum(row["gift_charging_head"] for row in rows),
            "giftCharger": sum(row["gift_charger"] for row in rows),
        }
        if user["role"] == "owner":
            summary["profit"] = sum(float(row["sale_price"])-float(row["purchase_cost_snapshot"]) for row in rows)
        else:
            for row in rows:
                row.pop("purchase_cost_snapshot", None)
                row["imei"] = ("••••" + row["imei"][-4:]) if row.get("imei") else ""
        return {"date": day, "rows": rows, "summary": summary, "role": user["role"]}

    def update_sale(self, user: dict, sale_id: str, data: dict) -> None:
        if user["role"] != "owner": raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以修改历史账目")
        try: price = float(data.get("salePrice"))
        except (TypeError, ValueError): raise ApiError(HTTPStatus.BAD_REQUEST, "成交价不正确")
        if price < 0: raise ApiError(HTTPStatus.BAD_REQUEST, "成交价不能小于0")
        truthy = lambda value: 1 if str(value).lower() in ("1", "true", "on", "yes") else 0
        stamp = now_iso()
        with WRITE_LOCK, connect() as db:
            before = db.execute("select sale_price,payment_method from sales where id=? and shop_id=?", (sale_id, user["shop_id"])).fetchone()
            cursor = db.execute("""update sales set sale_price=?,payment_method=?,customer_note=?,gift_case=?,gift_screen_protector=?,gift_charging_head=?,gift_charger=?,updated_at=?,updated_by=? where id=? and shop_id=?""",
                (price,str(data.get("paymentMethod","")),str(data.get("customerNote","")),truthy(data.get("giftCase")),truthy(data.get("giftScreenProtector")),truthy(data.get("giftChargingHead")),truthy(data.get("giftCharger")),stamp,user["id"],sale_id,user["shop_id"]))
            if not cursor.rowcount: raise ApiError(HTTPStatus.NOT_FOUND, "没有找到这笔销售记录")
            audit_insert(db, shop_id=user["shop_id"], action="sale_update", summary="更正销售账目", actor=user, entity_type="sale", entity_id=sale_id, details={"beforePrice": before["sale_price"] if before else None, "afterPrice": price}, client_ip=self.client_ip)
        self.send_json({"ok": True})

    def label_payload(self, device: sqlite3.Row) -> dict:
        qr_path = self.ensure_stock_qr(device)
        return {
            "Model": device["model"],
            "System": device["system_version"],
            "Storage": device["storage"],
            "Battery": str(device["battery_health"]) if device["battery_health"] is not None else "-",
            "Imei": device["imei"] or device["serial_number"] or "-",
            "Price": str(int(device["list_price"])) if float(device["list_price"]).is_integer() else str(device["list_price"]),
            "StockCode": device["stock_code"],
            "QrPath": str(qr_path),
        }

    def ensure_stock_qr(self, device: sqlite3.Row) -> Path:
        if qrcode is None: raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "二维码组件未安装")
        qr_dir = DB_PATH.parent / "stock-qrcodes"; qr_dir.mkdir(parents=True, exist_ok=True)
        path = qr_dir / f"{device['id']}-v2.png"
        if not path.exists():
            image = qrcode.make(device["stock_code"], border=4, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_H)
            image.save(path)
        return path

    def run_label_printer(self, device: sqlite3.Row, should_print: bool) -> Path:
        if not PRINT_AGENT.exists():
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "本地打印组件不存在")
        preview_dir = DB_PATH.parent / "label-previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / f"{device['id']}.png"
        encoded = base64.b64encode(json.dumps(self.label_payload(device), ensure_ascii=False).encode("utf-8")).decode("ascii")
        command = [str(PRINT_AGENT), "--payload", encoded, "--preview", str(preview_path), "--printer", "NIIMBOT B1"]
        if should_print:
            command.append("--print")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "打印失败").strip().splitlines()[-1]
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"打印机返回错误：{message[:180]}")
        return preview_path

    def print_device(self, user: dict, device_id: str) -> None:
        state = printer_state()
        if not state.get("connected"):
            raise ApiError(HTTPStatus.CONFLICT, f"打印机当前{state.get('status', '未连接')}，请检查电源、USB和Windows脱机状态后重试")
        stamp, job_id = now_iso(), str(uuid.uuid4())
        with connect() as db:
            device = db.execute("select * from devices where id=? and shop_id=? and deleted_at is null", (device_id, user["shop_id"])).fetchone()
            if not device:
                raise ApiError(HTTPStatus.NOT_FOUND, "没有找到设备")
            db.execute(
                "insert into print_jobs(id,shop_id,device_id,payload,status,attempts,requested_by,requested_at) values(?,?,?,?,?,?,?,?)",
                (job_id, user["shop_id"], device_id, json.dumps(self.label_payload(device), ensure_ascii=False), "printing", 1, user["id"], stamp),
            )
        try:
            self.run_label_printer(device, True)
        except ApiError as error:
            with connect() as db:
                db.execute("update print_jobs set status='failed',error_message=?,finished_at=? where id=?", (str(error), now_iso(), job_id))
                audit_insert(db, shop_id=user["shop_id"], action="label_print_failed", summary=f"标签打印失败：{device['stock_code']}", actor=user, entity_type="print_job", entity_id=job_id, details={"error": str(error)[:180]}, success=False, client_ip=self.client_ip)
            raise
        with connect() as db:
            db.execute("update print_jobs set status='printed',finished_at=? where id=?", (now_iso(), job_id))
            audit_insert(db, shop_id=user["shop_id"], action="label_print", summary=f"标签打印成功：{device['stock_code']}", actor=user, entity_type="print_job", entity_id=job_id, client_ip=self.client_ip)
        self.send_json({"ok": True, "jobId": job_id, "status": "printed"})

    def send_label_preview(self, user: dict, device_id: str) -> None:
        with connect() as db:
            device = db.execute("select * from devices where id=? and shop_id=? and deleted_at is null", (device_id, user["shop_id"])).fetchone()
        if not device:
            raise ApiError(HTTPStatus.NOT_FOUND, "没有找到设备")
        preview = self.run_label_printer(device, False)
        body = preview.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_device_photo(self, user: dict, photo_id: str) -> None:
        with connect() as db:
            row = db.execute("select file_path from device_photos where id=? and shop_id=?", (photo_id,user["shop_id"])).fetchone()
        if not row: raise ApiError(HTTPStatus.NOT_FOUND, "没有找到图片")
        path = (DB_PATH.parent / row["file_path"]).resolve()
        if DB_PATH.parent.resolve() not in path.parents or not path.exists(): raise ApiError(HTTPStatus.NOT_FOUND, "图片文件不存在")
        body = path.read_bytes(); content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","private,max-age=3600"); self.end_headers(); self.wfile.write(body)

    def add_device_photo(self,user:dict,device_id:str,data:dict)->None:
        encoded=str(data.get("image","")); match=re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$",encoded,re.I|re.S)
        if not match: raise ApiError(HTTPStatus.BAD_REQUEST,"请选择照片")
        try: body=base64.b64decode(match.group(2),validate=True)
        except ValueError: raise ApiError(HTTPStatus.BAD_REQUEST,"照片数据不正确")
        if not 1_000<=len(body)<=10_000_000: raise ApiError(HTTPStatus.BAD_REQUEST,"照片大小不正确")
        with connect() as db:
            if not db.execute("select 1 from devices where id=? and shop_id=?",(device_id,user["shop_id"])).fetchone(): raise ApiError(HTTPStatus.NOT_FOUND,"没有找到设备")
        suffix=".jpg" if match.group(1).lower() in ("jpeg","jpg") else "."+match.group(1).lower(); photo_id=str(uuid.uuid4()); directory=DB_PATH.parent/"device-photos"; directory.mkdir(parents=True,exist_ok=True); path=directory/f"{photo_id}{suffix}"; path.write_bytes(body); stamp=now_iso()
        description=str(data.get("description","")).strip() or "设备实拍图"
        with connect() as db:
            db.execute("insert into device_photos values(?,?,?,?,?,?,?,?)",(photo_id,user["shop_id"],device_id,"defect",str(path.relative_to(DB_PATH.parent)),description,user["id"],stamp))
            db.execute("insert into inventory_events(shop_id,device_id,event_type,from_status,to_status,note,actor_id,created_at) select shop_id,id,'photo_add',status,status,?,?,? from devices where id=?",(description,user["id"],stamp,device_id))
            audit_insert(db, shop_id=user["shop_id"], action="photo_add", summary="添加设备照片", actor=user, entity_type="device", entity_id=device_id, client_ip=self.client_ip)
        self.send_json({"ok":True,"id":photo_id,"description":description},HTTPStatus.CREATED)

    def list_events(self, user: dict) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("""select e.*,d.stock_code,d.model,d.storage,u.display_name actor_name from inventory_events e join devices d on d.id=e.device_id join users u on u.id=e.actor_id where e.shop_id=? order by e.created_at desc,e.id desc limit 100""", (user["shop_id"],))]

    def list_audit_events(self, user: dict) -> list[dict]:
        if user["role"] != "owner":
            raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以查看系统日志")
        query = parse_qs(urlparse(self.path).query)
        action = query.get("action", [""])[0].strip()
        text = query.get("q", [""])[0].strip()
        sql = "select id,actor_name,actor_role,action,entity_type,entity_id,summary,success,client_ip,created_at from audit_events where shop_id=?"
        args: list = [user["shop_id"]]
        if action:
            sql += " and action=?"; args.append(action)
        if text:
            sql += " and (summary like ? or actor_name like ?)"; args.extend([f"%{text}%", f"%{text}%"])
        sql += " order by created_at desc,id desc limit 200"
        with connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def report_summary(self,user:dict)->dict:
        query=parse_qs(urlparse(self.path).query); date_from=query.get("from",[datetime.now().strftime("%Y-%m-01")])[0]; date_to=query.get("to",[datetime.now().strftime("%Y-%m-%d")])[0]
        with connect() as db:
            sales=db.execute("""select count(*) count,coalesce(sum(s.sale_price),0) revenue,coalesce(sum(s.sale_price-s.purchase_cost_snapshot),0) profit from sales s where s.shop_id=? and date(s.sold_at,'localtime') between date(?) and date(?)""",(user["shop_id"],date_from,date_to)).fetchone()
            refunds=db.execute("""select count(*) count,coalesce(sum(r.refund_amount),0) amount,coalesce(sum(case when r.disposition='restock' then r.refund_amount-s.purchase_cost_snapshot else r.refund_amount end),0) profit_impact from returns r left join sales s on s.id=r.sale_id where r.shop_id=? and date(r.created_at,'localtime') between date(?) and date(?)""",(user["shop_id"],date_from,date_to)).fetchone()
            inventory=db.execute("""select count(*) count,coalesce(sum(f.purchase_cost),0) cost from devices d join device_financials f on f.device_id=d.id where d.shop_id=? and d.deleted_at is null and d.status in ('in_stock','reserved','sold_pending_pickup','in_repair','borrowed_for_test')""",(user["shop_id"],)).fetchone()
            aging=[dict(row) for row in db.execute("""select case when julianday('now')-julianday(created_at)>30 then '30天以上' when julianday('now')-julianday(created_at)>15 then '16-30天' when julianday('now')-julianday(created_at)>7 then '8-15天' else '0-7天' end bucket,count(*) count from devices where shop_id=? and deleted_at is null and status in ('in_stock','reserved') group by bucket order by min(julianday('now')-julianday(created_at))""",(user["shop_id"],))]
            models=[dict(row) for row in db.execute("""select s.model_snapshot model,s.storage_snapshot storage,count(*) count,coalesce(sum(s.sale_price),0) revenue from sales s where s.shop_id=? and date(s.sold_at,'localtime') between date(?) and date(?) group by s.model_snapshot,s.storage_snapshot order by count desc,revenue desc limit 20""",(user["shop_id"],date_from,date_to))]
            staff=[dict(row) for row in db.execute("""select u.display_name,count(*) sold_count,coalesce(sum(s.sale_price),0) revenue,coalesce(sum(s.sale_price-s.purchase_cost_snapshot),0) profit from sales s join users u on u.id=s.sold_by where s.shop_id=? and date(s.sold_at,'localtime') between date(?) and date(?) group by u.id,u.display_name order by revenue desc""",(user["shop_id"],date_from,date_to))]
        result={"from":date_from,"to":date_to,"soldCount":sales["count"],"revenue":sales["revenue"],"refundCount":refunds["count"],"refundAmount":refunds["amount"],"netRevenue":sales["revenue"]-refunds["amount"],"inventoryCount":inventory["count"],"aging":aging,"models":models,"staff":staff}
        if user["role"]=="owner": result.update({"grossProfit":sales["profit"],"netProfit":sales["profit"]-refunds["profit_impact"],"inventoryCost":inventory["cost"]})
        return result

    def parse_intake_text(self,user:dict,data:dict)->dict:
        text=str(data.get("text","")).strip()
        if not text: raise ApiError(HTTPStatus.BAD_REQUEST,"请先说话或输入描述")
        compact=re.sub(r"[,，。；;]"," ",text)
        result={"brand":"其他","model":"","storage":"","color":"","batteryHealth":None,"conditionGrade":"","purchaseCost":"","listPrice":"","imei":""}
        model_patterns=[(r"(?:苹果|iphone)\s*(\d+\s*(?:pro\s*max|pro|plus|mini)?)","Apple","iPhone "),(r"(华为\s*[A-Za-z0-9一-龥+ -]+)","华为",""),(r"(小米\s*[A-Za-z0-9一-龥+ -]+)","小米",""),(r"(OPPO\s*[A-Za-z0-9+ -]+)","OPPO",""),(r"(vivo\s*[A-Za-z0-9+ -]+)","vivo","")]
        for pattern,brand,prefix in model_patterns:
            match=re.search(pattern,compact,re.I)
            if match: result["brand"]=brand; result["model"]=(prefix+match.group(1).strip()).replace("  "," "); break
        storage=re.search(r"\b(32|64|128|256|512|1024)\s*[gG](?:[bB])?\b",compact)
        if storage: result["storage"]=storage.group(1)+"GB"
        for color in ("白色钛金属","原色钛金属","黑色钛金属","沙漠金","远峰蓝","黑色","白色","金色","银色","蓝色","绿色","紫色","红色","粉色"):
            if color in text: result["color"]=color; break
        battery=re.search(r"电池(?:健康)?\s*(\d{1,3})",compact)
        if battery: result["batteryHealth"]=min(100,int(battery.group(1)))
        condition=re.search(r"(全新|99新|98新|95新|9成新|8成新)",compact)
        if condition: result["conditionGrade"]=condition.group(1)
        cost=re.search(r"(?:成本|收货|进价)\s*(\d+(?:\.\d+)?)",compact)
        price=re.search(r"(?:卖|售价|标价|特价)\s*(\d+(?:\.\d+)?)",compact)
        if cost: result["purchaseCost"]=cost.group(1)
        if price: result["listPrice"]=price.group(1)
        imei=re.search(r"\b(\d{15})\b",compact)
        if imei: result["imei"]=imei.group(1)
        result["sourceText"]=text; result["mode"]="local-rules"; return result

    def market_filters(self) -> dict:
        query = parse_qs(urlparse(self.path).query)
        return {key: query.get(key, [""])[0].strip() for key in ("model", "storage", "conditionGrade", "batteryHealth", "repairStatus")}

    def require_market_owner(self, user: dict) -> None:
        if user["role"] != "owner":
            raise ApiError(HTTPStatus.FORBIDDEN, "只有老板可以查看和维护行情")

    def market_feed_status(self, user: dict) -> dict:
        self.require_market_owner(user)
        pages = []
        with connect() as db:
            for page_id, config in MARKET_FEED_PAGES.items():
                latest = db.execute("select * from market_feed_runs where shop_id=? and page_id=? order by created_at desc limit 1",(user["shop_id"],page_id)).fetchone()
                pages.append({"pageId":page_id,"sourceName":config["name"],"pageUrl":config["url"],"lastRun":dict(latest) if latest else None})
        return {"enabled":True,"schedule":"每天14:30，失败后每小时重试","busy":MARKET_FEED_LOCK.locked(),"pages":pages,"scope":"目前自动导入苹果有保、苹果无保；其他504x页面待逐版式校验后开放"}

    def sync_market_feed(self, user: dict, data: dict) -> None:
        self.require_market_owner(user)
        page_id = str(data.get("pageId", "")).strip()
        if page_id not in MARKET_FEED_PAGES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "目前只支持5041苹果有保和5042苹果无保")
        result = run_market_feed_page(user["shop_id"],user["id"],page_id)
        status = HTTPStatus.OK if result.get("ok") else (HTTPStatus.CONFLICT if "正在进行" in result.get("message", "") else HTTPStatus.UNPROCESSABLE_ENTITY)
        self.send_json(result,status)

    def list_market_quotes(self, user: dict) -> list[dict]:
        self.require_market_owner(user)
        filters = self.market_filters()
        sql = """select q.*,u.display_name creator_name from market_quotes q
                 join users u on u.id=q.created_by where q.shop_id=?"""
        args: list = [user["shop_id"]]
        if filters["model"]:
            sql += " and lower(q.model)=lower(?)"; args.append(filters["model"])
        if filters["storage"]:
            sql += " and lower(q.storage)=lower(?)"; args.append(filters["storage"])
        sql += " order by q.captured_on desc,q.created_at desc limit 200"
        with connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def recognize_market_sheet_url(self, user: dict, data: dict) -> None:
        self.require_market_owner(user)
        if recognize_market_sheet is None:
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "本地报价表识别组件没有安装")
        image_url = str(data.get("imageUrl", "")).strip()
        source_name = str(data.get("sourceName", "")).strip()
        parsed = urlparse(image_url)
        allowed_hosts = {"cos.huishoubaojiadan.com"}
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ApiError(HTTPStatus.BAD_REQUEST, "当前仅允许导入已确认的回收报价单图片域名")
        try:
            request = Request(image_url, headers={"User-Agent":"Mozilla/5.0 ZhangGui/1.0","Accept":"image/png,image/jpeg,image/webp"})
            with urlopen(request, timeout=25) as response:
                final_url = response.geturl()
                if urlparse(final_url).scheme != "https" or urlparse(final_url).hostname not in allowed_hosts:
                    raise ValueError("报价图片跳转到了未允许的地址")
                content_type = response.headers.get_content_type()
                body = response.read(15_000_001)
        except Exception as error:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"报价图片下载失败：{str(error)[:80]}")
        if len(body) > 15_000_000 or len(body) < 1_000 or content_type not in ("image/png","image/jpeg","image/webp"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "报价图片格式或大小不正确")
        if body.startswith(b"\x89PNG"):
            suffix = ".png"
        elif body.startswith(b"\xff\xd8"):
            suffix = ".jpg"
        elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "链接返回的不是支持的报价图片")
        directory = DB_PATH.parent / "market-sheets"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4()}{suffix}"
        path.write_bytes(body)
        try:
            result = recognize_market_sheet(path)
        except ValueError as error:
            path.unlink(missing_ok=True)
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(error))
        except Exception:
            path.unlink(missing_ok=True)
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "报价表本地识别失败，请换一张清晰原图")
        if not result.get("capturedOn"):
            timestamp = parse_qs(parsed.query).get("ts", [""])[0]
            if re.match(r"^20\d{6}", timestamp):
                result["capturedOn"] = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        result.update({"sourceName":source_name or "外部报价表","imageUrl":image_url,"sheetRef":str(path.relative_to(DB_PATH.parent)).replace("\\","/")})
        self.send_json(result)

    def import_market_sheet(self, user: dict, data: dict) -> None:
        self.require_market_owner(user)
        source_name = str(data.get("sourceName", "")).strip()
        image_url = str(data.get("imageUrl", "")).strip()
        sheet_ref = str(data.get("sheetRef", "")).strip().replace("\\", "/")
        rows = data.get("rows")
        try:
            captured_on = datetime.strptime(str(data.get("capturedOn", "")), "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "报价日期不正确")
        if not source_name or not isinstance(rows, list) or not rows or len(rows) > 200:
            raise ApiError(HTTPStatus.BAD_REQUEST, "报价表来源或识别行不正确")
        if not re.fullmatch(r"market-sheets/[0-9a-f-]{36}\.(?:png|jpg|webp)", sheet_ref, re.I):
            raise ApiError(HTTPStatus.BAD_REQUEST, "报价表原图引用不正确")
        sheet_path = (DB_PATH.parent / sheet_ref).resolve()
        sheet_directory = (DB_PATH.parent / "market-sheets").resolve()
        if sheet_path.parent != sheet_directory or not sheet_path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, "报价表原图不存在")
        quote_rows = []
        allowed_conditions = {"高保充新","靓机","小花","大花","外爆","内爆可测"}
        for row in rows:
            if not isinstance(row, dict): continue
            model, storage = str(row.get("model", "")).strip(), str(row.get("storage", "")).strip().upper()
            prices = row.get("prices") if isinstance(row.get("prices"), dict) else {}
            if not model or not storage: continue
            for condition, raw_price in prices.items():
                condition = str(condition).strip()
                try: price = float(raw_price)
                except (TypeError, ValueError): continue
                if condition in allowed_conditions and 50 <= price <= 50000:
                    quote_rows.append((model,storage,condition,price,str(row.get("networkModel", "")).strip()))
        if not quote_rows or len(quote_rows) > 1000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "没有可导入的有效报价")
        stamp, imported, skipped = now_iso(), 0, 0
        with WRITE_LOCK, connect() as db:
            db.execute("begin immediate")
            for model,storage,condition,price,network_model in quote_rows:
                exists = db.execute("""select 1 from market_quotes where shop_id=? and source_name=? and quote_type='recycle' and lower(model)=lower(?) and lower(storage)=lower(?) and condition_grade=? and price=? and captured_on=? limit 1""",(user["shop_id"],source_name,model,storage,condition,price,captured_on)).fetchone()
                if exists:
                    skipped += 1; continue
                note = "报价表批量导入" + (f" · 网络型号{network_model}" if network_model else "")
                db.execute("""insert into market_quotes(id,shop_id,source_name,quote_type,brand,model,storage,condition_grade,battery_health,repair_status,price,captured_on,note,created_by,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),user["shop_id"],source_name,"recycle","Apple",model,storage,condition,None,"unknown",price,captured_on,note,user["id"],stamp))
                imported += 1
            db.execute("""insert into market_sheet_imports(id,shop_id,source_name,captured_on,image_url,file_path,row_count,quote_count,created_by,created_at) values(?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),user["shop_id"],source_name,captured_on,image_url,sheet_ref,len(rows),imported,user["id"],stamp))
        self.send_json({"ok":True,"imported":imported,"skipped":skipped,"rowCount":len(rows)},HTTPStatus.CREATED)

    def create_market_quote(self, user: dict, data: dict) -> None:
        self.require_market_owner(user)
        source = str(data.get("sourceName", "")).strip()
        quote_type = str(data.get("quoteType", "")).strip()
        model = str(data.get("model", "")).strip()
        storage = str(data.get("storage", "")).strip().upper()
        repair_status = str(data.get("repairStatus", "unknown")).strip() or "unknown"
        try:
            price = float(data.get("price") or 0)
            battery = int(data["batteryHealth"]) if str(data.get("batteryHealth", "")).strip() else None
            captured_on = datetime.strptime(str(data.get("capturedOn", "")), "%Y-%m-%d").date().isoformat()
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "价格、电池或报价日期格式不正确")
        if not source or not model or not storage or price <= 0 or quote_type not in ("recycle", "retail"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请完整填写来源、报价类型、型号、容量和价格")
        if battery is not None and not 0 <= battery <= 100:
            raise ApiError(HTTPStatus.BAD_REQUEST, "电池健康应为0到100")
        if repair_status not in ("original", "no_repair", "minor_repair", "major_repair", "unknown"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "拆修情况不正确")
        quote_id, stamp = str(uuid.uuid4()), now_iso()
        with WRITE_LOCK, connect() as db:
            db.execute("""insert into market_quotes(id,shop_id,source_name,quote_type,brand,model,storage,condition_grade,battery_health,repair_status,price,captured_on,note,created_by,created_at)
                          values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (quote_id,user["shop_id"],source,quote_type,str(data.get("brand", "")).strip(),model,storage,str(data.get("conditionGrade", "")).strip(),battery,repair_status,price,captured_on,str(data.get("note", "")).strip(),user["id"],stamp))
        self.send_json({"ok": True, "id": quote_id}, HTTPStatus.CREATED)

    def delete_market_quote(self, user: dict, quote_id: str) -> None:
        self.require_market_owner(user)
        with WRITE_LOCK, connect() as db:
            cursor = db.execute("delete from market_quotes where id=? and shop_id=?", (quote_id,user["shop_id"]))
            if not cursor.rowcount:
                raise ApiError(HTTPStatus.NOT_FOUND, "行情记录不存在")
        self.send_json({"ok": True})

    def market_summary(self, user: dict) -> dict:
        self.require_market_owner(user)
        filters = self.market_filters()
        model, storage = filters["model"], filters["storage"].upper()
        if not model:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先填写需要查询的型号")
        quote_sql = "select quote_type,price,source_name,captured_on from market_quotes where shop_id=? and lower(model)=lower(?)"
        quote_args: list = [user["shop_id"], model]
        if storage:
            quote_sql += " and lower(storage)=lower(?)"; quote_args.append(storage)
        if filters["conditionGrade"]:
            # Store the source's wording unchanged, but let the shop's familiar
            # condition grades match the closest tier in a recycle quote sheet.
            sheet_grade = {
                "全新": "靓机", "99新": "靓机", "95新": "靓机",
                "9成新": "小花", "8成新": "大花",
            }.get(filters["conditionGrade"])
            if sheet_grade:
                quote_sql += " and (condition_grade in (?,?) or condition_grade='')"
                quote_args.extend((filters["conditionGrade"], sheet_grade))
            else:
                quote_sql += " and (condition_grade=? or condition_grade='')"
                quote_args.append(filters["conditionGrade"])
        if filters["batteryHealth"]:
            try: battery = int(filters["batteryHealth"])
            except ValueError: raise ApiError(HTTPStatus.BAD_REQUEST, "电池健康格式不正确")
            quote_sql += " and (battery_health is null or abs(battery_health-?)<=5)"; quote_args.append(battery)
        if filters["repairStatus"] and filters["repairStatus"] != "unknown":
            quote_sql += " and (repair_status=? or repair_status='unknown')"; quote_args.append(filters["repairStatus"])
        quote_sql += " order by captured_on desc limit 200"
        device_filter = "d.shop_id=? and lower(d.model)=lower(?)"
        device_args: list = [user["shop_id"], model]
        if storage:
            device_filter += " and lower(d.storage)=lower(?)"; device_args.append(storage)
        with connect() as db:
            quotes = [dict(row) for row in db.execute(quote_sql, quote_args)]
            sales = [float(row[0]) for row in db.execute(f"select s.sale_price from sales s join devices d on d.id=s.device_id where {device_filter} order by s.sold_at desc limit 50", device_args)]
            inventory = [dict(row) for row in db.execute(f"select d.list_price,f.purchase_cost from devices d join device_financials f on f.device_id=d.id where {device_filter} and d.deleted_at is null order by d.created_at desc limit 50", device_args)]
        recycle = [row["price"] for row in quotes if row["quote_type"] == "recycle"]
        retail = [row["price"] for row in quotes if row["quote_type"] == "retail"]
        recycle_median, retail_median = median_value(recycle), median_value(retail)
        sales_median = median_value(sales)
        list_median = median_value([row["list_price"] for row in inventory])
        cost_median = median_value([row["purchase_cost"] for row in inventory])
        purchase_center = recycle_median or cost_median or (sales_median * .82 if sales_median else None) or (retail_median * .78 if retail_median else None)
        sale_evidence = [value for value in (retail_median, sales_median, list_median) if value]
        sale_center = (sum(sale_evidence) / len(sale_evidence)) if sale_evidence else (recycle_median * 1.15 if recycle_median else None)
        purchase_range, sale_range = rounded_range(purchase_center, .04), rounded_range(sale_center, .04)
        evidence_count = len(quotes) + len(sales)
        confidence = "high" if evidence_count >= 8 and bool(recycle) and bool(retail or sales) else ("medium" if evidence_count >= 3 else "low")
        sources = sorted({row["source_name"] for row in quotes})
        basis = []
        if recycle: basis.append(f"外部回收价{len(recycle)}条")
        if retail: basis.append(f"外部零售价{len(retail)}条")
        if sales: basis.append(f"店内成交{len(sales)}笔")
        if inventory: basis.append(f"当前同款库存{len(inventory)}台")
        return {
            "query": filters,
            "external": {"recycleCount":len(recycle),"recycleMedian":recycle_median,"retailCount":len(retail),"retailMedian":retail_median,"sources":sources},
            "internal": {"salesCount":len(sales),"salesMedian":sales_median,"inventoryCount":len(inventory),"listMedian":list_median,"costMedian":cost_median},
            "suggestion": {"purchaseRange":purchase_range,"saleRange":sale_range,"estimatedMargin":(sale_range[0]-purchase_range[1]) if sale_range and purchase_range else None,"confidence":confidence,"basis":"、".join(basis) or "暂无同款证据"},
            "notice": "建议价只用于辅助判断，外部行情与门店历史相互独立，最终价格由老板确认。",
            "mode": "local-evidence"
        }

    def create_pricing_decision(self, user: dict, data: dict) -> None:
        self.require_market_owner(user)
        model, storage = str(data.get("model", "")).strip(), str(data.get("storage", "")).strip().upper()
        try:
            final_purchase = float(data["finalPurchasePrice"]) if str(data.get("finalPurchasePrice", "")).strip() else None
            final_sale = float(data["finalSalePrice"]) if str(data.get("finalSalePrice", "")).strip() else None
            battery = int(data["batteryHealth"]) if str(data.get("batteryHealth", "")).strip() else None
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "最终价格或电池健康格式不正确")
        if not model or not storage or (final_purchase is None and final_sale is None):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请填写型号、容量以及至少一个最终价格")
        if any(value is not None and value < 0 for value in (final_purchase, final_sale)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "最终价格不能小于0")
        suggestion = data.get("suggestion") if isinstance(data.get("suggestion"), dict) else {}
        purchase_range = suggestion.get("purchaseRange") if isinstance(suggestion.get("purchaseRange"), list) else [None,None]
        sale_range = suggestion.get("saleRange") if isinstance(suggestion.get("saleRange"), list) else [None,None]
        evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        stamp, decision_id = now_iso(), str(uuid.uuid4())
        with WRITE_LOCK, connect() as db:
            device_id = str(data.get("deviceId", "")).strip() or None
            if device_id and not db.execute("select 1 from devices where id=? and shop_id=?",(device_id,user["shop_id"])).fetchone():
                raise ApiError(HTTPStatus.BAD_REQUEST, "关联库存设备不存在")
            db.execute("""insert into pricing_decisions(id,shop_id,device_id,brand,model,storage,condition_grade,battery_health,repair_status,suggested_purchase_low,suggested_purchase_high,suggested_sale_low,suggested_sale_high,final_purchase_price,final_sale_price,adjustment_reason,evidence_snapshot,created_by,created_at)
                          values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (decision_id,user["shop_id"],device_id,str(data.get("brand", "")).strip(),model,storage,str(data.get("conditionGrade", "")).strip(),battery,str(data.get("repairStatus", "unknown")),purchase_range[0] if len(purchase_range)>0 else None,purchase_range[1] if len(purchase_range)>1 else None,sale_range[0] if len(sale_range)>0 else None,sale_range[1] if len(sale_range)>1 else None,final_purchase,final_sale,str(data.get("adjustmentReason", "")).strip(),json.dumps(evidence,ensure_ascii=False)[:20000],user["id"],stamp))
        self.send_json({"ok": True, "id": decision_id}, HTTPStatus.CREATED)

    def list_pricing_decisions(self, user: dict) -> list[dict]:
        self.require_market_owner(user)
        filters = self.market_filters()
        sql = "select p.*,u.display_name creator_name from pricing_decisions p join users u on u.id=p.created_by where p.shop_id=?"
        args: list = [user["shop_id"]]
        if filters["model"]:
            sql += " and lower(p.model)=lower(?)"; args.append(filters["model"])
        if filters["storage"]:
            sql += " and lower(p.storage)=lower(?)"; args.append(filters["storage"])
        sql += " order by p.created_at desc limit 100"
        with connect() as db:
            rows = [dict(row) for row in db.execute(sql,args)]
        for row in rows: row.pop("evidence_snapshot", None)
        return rows

    def price_suggestion(self,user:dict,device_id:str)->dict:
        with connect() as db:
            device=db.execute("select d.*,f.purchase_cost from devices d join device_financials f on f.device_id=d.id where d.id=? and d.shop_id=?",(device_id,user["shop_id"])).fetchone()
            if not device: raise ApiError(HTTPStatus.NOT_FOUND,"没有找到设备")
            history=[row[0] for row in db.execute("select s.sale_price from sales s join devices d on d.id=s.device_id where s.shop_id=? and d.model=? and d.storage=? order by s.sold_at desc limit 20",(user["shop_id"],device["model"],device["storage"]))]
        if history:
            ordered=sorted(history); base=ordered[len(ordered)//2]; basis=f"参考店内{len(history)}笔同型号成交"
        else: base=float(device["list_price"]); basis="暂无同型号成交，参考当前标价"
        battery=device["battery_health"] or 100
        if battery<80: base-=200
        elif battery<85: base-=100
        age=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(device["created_at"])).days)
        if age>30: base*=.96
        elif age>15: base*=.98
        suggested=max(float(device["purchase_cost"]),round(base/10)*10)
        return {"suggestedPrice":suggested,"minimumPrice":max(float(device["purchase_cost"]),round(float(device["purchase_cost"])*1.03/10)*10),"basis":basis,"ageDays":age,"historyCount":len(history),"mode":"local-data"}

    def sales_copy(self,user:dict,device_id:str)->dict:
        with connect() as db: device=db.execute("select * from devices where id=? and shop_id=?",(device_id,user["shop_id"])).fetchone()
        if not device: raise ApiError(HTTPStatus.NOT_FOUND,"没有找到设备")
        battery=f"，电池健康{device['battery_health']}%" if device["battery_health"] is not None else ""
        text=f"【现货】{device['model']} {device['storage']} {device['color']}，{device['condition_grade']}{battery}，系统{device['system_version'] or '正常'}，特价¥{int(device['list_price']) if float(device['list_price']).is_integer() else device['list_price']}。支持到店验机，机器信息以现场为准。"
        return {"text":text,"privacy":"已隐藏成本和完整IMEI","mode":"local-template"}

    def smart_daily_summary(self,user:dict)->dict:
        with connect() as db:
            intake=db.execute("select count(*) from devices where shop_id=? and date(created_at,'localtime')=date('now','localtime')",(user["shop_id"],)).fetchone()[0]
            sold=db.execute("select count(*),coalesce(sum(sale_price),0) from sales where shop_id=? and date(sold_at,'localtime')=date('now','localtime')",(user["shop_id"],)).fetchone()
            aged=db.execute("select count(*) from devices where shop_id=? and status in ('in_stock','reserved') and julianday('now')-julianday(created_at)>30",(user["shop_id"],)).fetchone()[0]
            below=db.execute("select count(*) from devices d join device_financials f on f.device_id=d.id where d.shop_id=? and d.deleted_at is null and d.status in ('in_stock','reserved') and d.list_price<f.purchase_cost",(user["shop_id"],)).fetchone()[0]
        lines=[f"今天入库{intake}台，售出{sold[0]}台，销售额¥{sold[1]:,.0f}。"]
        if aged: lines.append(f"有{aged}台库存超过30天，建议优先检查价格。")
        if below: lines.append(f"有{below}台标价低于成本，请老板复核。")
        if not aged and not below: lines.append("当前没有久库或低于成本的异常提醒。")
        return {"summary":"".join(lines),"intake":intake,"sold":sold[0],"revenue":sold[1],"aged":aged,"belowCost":below,"mode":"local-data"}

    def send_csv(self,filename:str,headers:list[str],rows:list[list])->None:
        output=io.StringIO(); writer=csv.writer(output); writer.writerow(headers); writer.writerows(rows); body=("\ufeff"+output.getvalue()).encode("utf-8")
        self.send_response(HTTPStatus.OK); self.send_header("Content-Type","text/csv; charset=utf-8"); self.send_header("Content-Disposition",f'attachment; filename="{filename}"'); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)

    def export_devices(self,user:dict)->None:
        with connect() as db:
            rows=db.execute("""select d.stock_code,d.brand,d.model,d.storage,d.color,d.system_version,d.battery_health,d.charge_cycles,d.condition_grade,d.imei,d.serial_number,d.status,d.area,d.list_price,f.purchase_cost,d.created_at from devices d left join device_financials f on f.device_id=d.id where d.shop_id=? and d.deleted_at is null order by d.created_at desc""",(user["shop_id"],)).fetchall()
        headers=["库存编号","品牌","型号","容量","颜色","系统","电池健康","充电次数","成色","IMEI","序列号","状态","库位","标价"]
        data=[]
        for row in rows:
            values=list(row); base=values[:14]
            if user["role"]=="owner": base.extend([values[14],values[15]])
            else: base.append(values[15])
            data.append(base)
        final_headers=(headers+["收货成本","入库时间"]) if user["role"]=="owner" else (headers+["入库时间"])
        self.send_csv("devices.csv",final_headers,data)

    def export_sales(self,user:dict)->None:
        with connect() as db:
            rows=db.execute("""select d.stock_code,s.model_snapshot,s.storage_snapshot,substr(s.imei_snapshot,-4),s.sale_price,s.purchase_cost_snapshot,s.payment_method,u.display_name,s.sold_at from sales s join devices d on d.id=s.device_id join users u on u.id=s.sold_by where s.shop_id=? order by s.sold_at desc""",(user["shop_id"],)).fetchall()
        headers=["库存编号","型号","容量","IMEI尾号","成交价"]+(["收货成本","毛利"] if user["role"]=="owner" else [])+["付款方式","销售人员","销售时间"]
        data=[]
        for row in rows:
            base=list(row[:5]);
            if user["role"]=="owner": base += [row[5],row[4]-row[5]]
            base += list(row[6:]); data.append(base)
        self.send_csv("sales.csv",headers,data)

    def export_ledger(self,user:dict)->None:
        query=parse_qs(urlparse(self.path).query); day=query.get("date",[datetime.now().strftime("%Y-%m-%d")])[0]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",day): raise ApiError(HTTPStatus.BAD_REQUEST,"日期格式不正确")
        with connect() as db:
            rows=db.execute("""select s.sold_at,s.model_snapshot,s.storage_snapshot,s.imei_snapshot,s.sale_price,s.purchase_cost_snapshot,
                s.gift_case,s.gift_screen_protector,s.gift_charging_head,s.gift_charger,s.payment_method,u.display_name,s.customer_note
                from sales s join users u on u.id=s.sold_by where s.shop_id=? and date(s.sold_at,'localtime')=date(?) order by s.sold_at""",(user["shop_id"],day)).fetchall()
        headers=["日期时间","型号","容量","串号","成交价"]+(["回收价","利润"] if user["role"]=="owner" else [])+["送壳","送膜","送充电头","送充电器","付款方式","销售人员","备注"]
        data=[]
        for row in rows:
            serial=row[3] if user["role"]=="owner" else (("••••"+row[3][-4:]) if row[3] else "")
            base=[row[0],row[1],row[2],serial,row[4]]
            if user["role"]=="owner": base += [row[5],row[4]-row[5]]
            base += [("是" if value else "否") for value in row[6:10]]+list(row[10:])
            data.append(base)
        self.send_csv(f"ledger-{day}.csv",headers,data)

    def import_devices_csv(self,user:dict,data:dict)->None:
        text=str(data.get("csv","")).lstrip("\ufeff")
        if not text.strip(): raise ApiError(HTTPStatus.BAD_REQUEST,"CSV文件为空")
        aliases={"品牌":"brand","型号":"model","容量":"storage","颜色":"color","系统":"systemVersion","系统版本":"systemVersion","电池健康":"batteryHealth","充电次数":"chargeCycles","成色":"conditionGrade","IMEI":"imei","IMEI2":"imei2","序列号":"serialNumber","库位":"area","标价":"listPrice","销售标价":"listPrice","收货成本":"purchaseCost","成本":"purchaseCost","备注":"notes"}
        reader=csv.DictReader(io.StringIO(text)); rows=[]
        for source in reader:
            row={aliases.get(str(key).strip(),str(key).strip()):str(value or "").strip() for key,value in source.items() if key}
            rows.append(row)
        if not rows or len(rows)>500: raise ApiError(HTTPStatus.BAD_REQUEST,"CSV应包含1到500条数据")
        imported=[]; errors=[]; stamp=now_iso()
        with WRITE_LOCK,connect() as db:
            for index,row in enumerate(rows,start=2):
                try:
                    imei=re.sub(r"\D","",row.get("imei","")); required=(row.get("brand"),row.get("model"),row.get("storage"),imei,row.get("purchaseCost"),row.get("listPrice"))
                    if not all(required) or len(imei)!=15: raise ValueError("品牌、型号、容量、15位IMEI、成本和标价为必填")
                    cost,price=float(row["purchaseCost"]),float(row["listPrice"]); device_id=str(uuid.uuid4()); code=self.next_stock_code(db,user["shop_id"],row["brand"])
                    db.execute("""insert into devices(id,shop_id,stock_code,brand,model,storage,color,system_version,battery_health,charge_cycles,condition_grade,list_price,imei,imei2,serial_number,area,notes,source_fields,created_by,updated_by,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(device_id,user["shop_id"],code,row["brand"],row["model"],row["storage"],row.get("color",""),row.get("systemVersion",""),int(row["batteryHealth"]) if row.get("batteryHealth") else None,int(row["chargeCycles"]) if row.get("chargeCycles") else None,row.get("conditionGrade",""),price,imei,row.get("imei2") or None,row.get("serialNumber") or None,row.get("area") or "默认区",row.get("notes",""),json.dumps({"source":"csv_import"},ensure_ascii=False),user["id"],user["id"],stamp,stamp))
                    db.execute("insert into device_financials values(?,?,?,?,?,?)",(device_id,user["shop_id"],cost,None,user["id"],stamp)); db.execute("insert into inventory_events(shop_id,device_id,event_type,to_status,note,actor_id,created_at) values(?,?,?,?,?,?,?)",(user["shop_id"],device_id,"csv_import","in_stock",f"CSV第{index}行",user["id"],stamp)); imported.append({"row":index,"stockCode":code})
                except (ValueError,sqlite3.IntegrityError) as error:
                    errors.append({"row":index,"error":"IMEI重复" if isinstance(error,sqlite3.IntegrityError) else str(error)})
            audit_insert(db, shop_id=user["shop_id"], action="inventory_csv_import", summary=f"批量导入库存：成功{len(imported)}条，失败{len(errors)}条", actor=user, entity_type="import", details={"imported": len(imported), "failed": len(errors)}, client_ip=self.client_ip)
        self.send_json({"ok":True,"imported":len(imported),"errors":errors,"items":imported})

    def send_qr(self) -> None:
        if qrcode is None: raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "二维码组件未安装")
        image = qrcode.make(self.phone_url, image_factory=qrcode.image.svg.SvgPathImage, border=2); output = io.BytesIO(); image.save(output); body=output.getvalue()
        self.send_response(HTTPStatus.OK); self.send_header("Content-Type","image/svg+xml"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # The shop server normally runs in a hidden window.  Writing the
        # standard static-file access log to a detached stderr stream can
        # raise OSError on Windows before the response headers are sent,
        # leaving the browser with a blank page.  Application failures are
        # still reported explicitly by the exception handlers above.
        return


def main() -> None:
    global DB_PATH
    parser=argparse.ArgumentParser(); parser.add_argument("--port",type=int,default=4180); parser.add_argument("--open",action="store_true"); parser.add_argument("--db"); parser.add_argument("--reset-password",metavar="USERNAME"); args=parser.parse_args()
    if args.db: DB_PATH = Path(args.db).resolve()
    init_db(); database_check(); daily_backup()
    if args.reset_password:
        first = getpass.getpass("请输入新密码（至少6位）：")
        second = getpass.getpass("请再次输入新密码：")
        if len(first) < 6 or first != second:
            raise SystemExit("密码不足6位或两次输入不一致")
        digest, salt = hash_password(first)
        with connect() as db:
            row = db.execute("select id from users where username=? collate nocase", (args.reset_password,)).fetchone()
            if not row: raise SystemExit("没有找到该用户名")
            db.execute("update users set password_hash=?,password_salt=? where id=?", (digest, salt, row["id"]))
            db.execute("delete from sessions where user_id=?", (row["id"],))
        print("密码重置成功，请重新登录。")
        return
    server=ThreadingHTTPServer(("0.0.0.0",args.port),StoreHandler)
    service_stop = threading.Event()
    market_thread = backup_thread = None
    if not args.db:
        market_thread = threading.Thread(target=market_feed_scheduler,args=(service_stop,),name="market-feed",daemon=True)
        market_thread.start()
        backup_thread = threading.Thread(target=backup_scheduler,args=(service_stop,),name="daily-backup",daemon=True)
        backup_thread.start()
    LOGGER.info("server started port=%s database=%s", args.port, DB_PATH)
    print(f"\n掌柜台第一版已启动\n电脑：http://127.0.0.1:{args.port}/\n手机：http://{lan_ip()}:{args.port}/\n")
    if args.open: threading.Timer(.8,lambda:webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        service_stop.set()
        server.server_close()


if __name__ == "__main__": main()
