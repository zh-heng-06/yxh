from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


ANDROID_READ_LOCK = threading.Lock()
KNOWN_STORAGE_GB = (16, 32, 64, 128, 256, 512, 1024, 2048)
SERVER_DIR = Path(__file__).resolve().parent


class AndroidDeviceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _adb_path() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("ZHANGGUI_ADB_PATH", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            SERVER_DIR / "tools" / "android-platform-tools" / "platform-tools" / "adb.exe",
            SERVER_DIR / "tools" / "android-platform-tools" / "adb.exe",
        ]
    )
    for root_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = os.environ.get(root_name, "").strip()
        if root:
            candidates.append(Path(root) / "platform-tools" / "adb.exe")
    located = shutil.which("adb")
    if located:
        candidates.append(Path(located))
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        candidates.append(Path(local_app) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AndroidDeviceError(
        "dependency_missing",
        "安卓连接组件尚未安装，请在项目目录双击“安装安卓连接组件.cmd”，完成后重启系统",
    )


def _run_adb(arguments: list[str], timeout: float = 10) -> str:
    adb = _adb_path()
    try:
        completed = subprocess.run(
            [str(adb), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise AndroidDeviceError("timeout", "安卓设备读取超时，请保持手机解锁并重新插拔数据线") from error
    except OSError as error:
        raise AndroidDeviceError("driver_error", "安卓连接组件无法启动，请重新安装连接组件") from error
    output = (completed.stdout or "").strip()
    detail = (completed.stderr or "").strip()
    if completed.returncode and not output:
        lowered = detail.lower()
        if "unauthorized" in lowered:
            raise AndroidDeviceError("unauthorized", "请解锁手机，在手机上允许这台电脑进行USB调试")
        if "offline" in lowered:
            raise AndroidDeviceError("offline", "手机连接已离线，请重新插拔数据线")
        if "no devices" in lowered or "not found" in lowered:
            raise AndroidDeviceError("no_device", "没有检测到已授权的安卓手机")
        raise AndroidDeviceError("read_failed", f"安卓设备没有完成读取：{detail[:160] or 'ADB命令失败'}")
    return output


def _parse_devices(output: str) -> list[dict]:
    devices: list[dict] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        attributes: dict[str, str] = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                attributes[key] = value.replace("_", " ")
        model = attributes.get("model") or attributes.get("device") or "安卓手机"
        devices.append(
            {
                "serial": serial,
                "serialTail": serial[-8:],
                "state": state,
                "model": model,
                "label": f"{model}（设备号尾段 {serial[-8:]}）",
                "connectionType": "Wi-Fi" if ":" in serial else "USB",
            }
        )
    return devices


def android_usb_status() -> dict:
    try:
        devices = _parse_devices(_run_adb(["devices", "-l"], 8))
    except AndroidDeviceError as error:
        return {"available": False, "state": error.code, "message": str(error), "devices": []}
    if not devices:
        return {
            "available": True,
            "state": "no_device",
            "message": "没有检测到安卓/华为手机，请连接数据线并开启USB调试",
            "devices": [],
        }
    ready = [device for device in devices if device["state"] == "device"]
    if ready:
        return {
            "available": True,
            "state": "connected",
            "message": f"检测到{len(ready)}台已授权的安卓/华为手机",
            "devices": ready,
        }
    if any(device["state"] == "unauthorized" for device in devices):
        return {
            "available": True,
            "state": "unauthorized",
            "message": "已检测到手机，请解锁并在手机上点“允许USB调试”",
            "devices": devices,
        }
    return {
        "available": True,
        "state": "offline",
        "message": "手机已连接但当前离线，请重新插拔数据线并保持手机解锁",
        "devices": devices,
    }


def _parse_getprop(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\[([^]]+)\]:\s*\[(.*)\]", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _first(values: dict[str, str], *keys: str) -> str:
    return next((values[key].strip() for key in keys if values.get(key, "").strip()), "")


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _brand_label(manufacturer: str, brand: str) -> str:
    source = f"{manufacturer} {brand}".lower()
    mappings = (
        (("huawei", "华为"), "华为"),
        (("honor", "荣耀"), "荣耀"),
        (("xiaomi", "redmi", "poco", "小米"), "小米"),
        (("oppo",), "OPPO"),
        (("oneplus",), "OnePlus"),
        (("vivo", "iqoo"), "vivo"),
        (("samsung",), "三星"),
        (("realme",), "realme"),
        (("meizu",), "魅族"),
        (("google",), "Google"),
        (("motorola", "lenovo"), "摩托罗拉"),
    )
    for needles, label in mappings:
        if any(needle in source for needle in needles):
            return label
    return manufacturer.strip() or brand.strip() or "其他"


def _storage_label(total_kb: Any) -> str:
    total = _safe_int(total_kb)
    if not total:
        return ""
    binary_gb = total * 1024 / 1_000_000_000
    closest = min(KNOWN_STORAGE_GB, key=lambda value: abs(value - binary_gb))
    # /data 通常不含系统分区，允许比标称容量少一些，但拒绝明显不可信的结果。
    if binary_gb < closest * 0.68 or binary_gb > closest * 1.12:
        return ""
    return f"{closest}GB" if closest < 1024 else f"{closest // 1024}TB"


def _storage_total_kb(df_output: str) -> int | None:
    for line in reversed(df_output.splitlines()):
        columns = line.split()
        if len(columns) >= 4 and (columns[-1] == "/data" or "/data" in columns[-1]):
            return _safe_int(columns[1])
    return None


def _imei_values(properties: dict[str, str]) -> list[str]:
    values: list[str] = []
    for key, raw in properties.items():
        if "imei" not in key.lower():
            continue
        for match in re.findall(r"(?<!\d)\d{15}(?!\d)", raw):
            if match not in values and match != "0" * 15:
                values.append(match)
    return values[:2]


def _battery_values(properties: dict[str, str], battery_text: str, sysfs_text: str) -> tuple[int | None, int | None, dict]:
    metrics: dict[str, int] = {}
    for line in sysfs_text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed = _safe_int(value)
        if parsed is not None:
            metrics[key.strip()] = parsed
    for line in battery_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed = _safe_int(value)
        if parsed is not None:
            metrics.setdefault(f"dumpsys_{key.strip().replace(' ', '_')}", parsed)

    health = next((metrics[key] for key in ("soh", "battery_soh", "fg_fullcapnom_percent") if 1 <= metrics.get(key, 0) <= 100), None)
    full = next((metrics[key] for key in ("charge_full", "fcc", "battery_fcc") if metrics.get(key, 0) > 0), None)
    design = next((metrics[key] for key in ("charge_full_design", "design_capacity", "battery_design_capacity") if metrics.get(key, 0) > 0), None)
    if health is None and full and design:
        ratio = round(full / design * 100)
        if 1 <= ratio <= 110:
            health = min(100, ratio)
    cycles = next((metrics[key] for key in ("cycle_count", "battery_cycle_count", "fg_cycle") if 0 <= metrics.get(key, -1) <= 10000), None)
    return health, cycles, {"batteryMetrics": metrics}


def normalize_android_payload(
    properties: dict[str, str],
    storage_total_kb: int | None,
    battery_text: str,
    sysfs_text: str,
    serial: str,
) -> dict:
    manufacturer = _first(properties, "ro.product.manufacturer", "ro.product.vendor.manufacturer")
    raw_brand = _first(properties, "ro.product.brand", "ro.product.vendor.brand")
    brand = _brand_label(manufacturer, raw_brand)
    model = _first(
        properties,
        "ro.config.marketing_name",
        "ro.product.marketname",
        "ro.vendor.product.display",
        "ro.product.model",
        "ro.product.vendor.model",
        "ro.product.device",
    ) or "安卓手机"
    harmony = _first(
        properties,
        "hw_sc.build.platform.version",
        "ro.build.version.harmony",
        "ro.huawei.build.version.harmony",
    )
    emui = _first(properties, "ro.build.version.emui", "ro.build.hw_emui_api_level")
    magic = _first(properties, "ro.build.version.magic", "ro.build.version.magicui")
    hyper = _first(properties, "ro.mi.os.version.name", "ro.miui.ui.version.name")
    android = _first(properties, "ro.build.version.release")
    if harmony:
        system_version = harmony if "harmony" in harmony.lower() else f"HarmonyOS {harmony}"
    elif emui:
        system_version = emui.replace("EmotionUI_", "EMUI ")
    elif magic:
        system_version = magic if "magic" in magic.lower() else f"MagicOS {magic}"
    elif hyper:
        system_version = hyper if "hyper" in hyper.lower() else f"HyperOS {hyper}"
    else:
        system_version = f"Android {android}" if android else ""
    imeis = _imei_values(properties)
    health, cycles, battery_source = _battery_values(properties, battery_text, sysfs_text)
    warnings: list[str] = []
    if not imeis:
        warnings.append("系统权限未开放IMEI，请从拨号界面输入*#06#后人工核对")
    if health is None:
        warnings.append("厂商未开放电池健康，请用品牌验机页或店内检测工具人工确认")
    if cycles is None:
        warnings.append("厂商未开放充电次数，请人工确认")
    storage = _storage_label(storage_total_kb)
    if not storage:
        warnings.append("容量未能可靠换算，请在手机设置中人工确认")
    source_fields = {
        "mode": "android_usb",
        "provider": "Google Android Platform Tools (ADB)",
        "connection": "Wi-Fi" if ":" in serial else "USB",
        "serialTail": serial[-8:],
        "manufacturer": manufacturer,
        "rawBrand": raw_brand,
        "productDevice": _first(properties, "ro.product.device"),
        "buildDisplay": _first(properties, "ro.build.display.id"),
        "androidVersion": android,
        "sdkLevel": _first(properties, "ro.build.version.sdk"),
        "storageDataTotalKB": storage_total_kb,
        **battery_source,
        "readWarnings": warnings,
    }
    return {
        "brand": brand,
        "model": model,
        "storage": storage,
        "color": "",
        "systemVersion": system_version,
        "androidVersion": android,
        "batteryHealth": health,
        "chargeCycles": cycles,
        "imei": imeis[0] if imeis else "",
        "imei2": imeis[1] if len(imeis) > 1 else "",
        "serialNumber": _first(properties, "ro.serialno", "ro.boot.serialno") or serial,
        "serial": serial,
        "manufacturer": manufacturer,
        "warnings": warnings,
        "sourceFields": source_fields,
        "privacy": "仅读取设备型号、系统、容量和厂商允许读取的电池/标识资料，未读取照片、通讯录、消息或账号内容",
    }


def _shell(serial: str, command: str, timeout: float = 10) -> str:
    return _run_adb(["-s", serial, "shell", command], timeout)


def _read_android(serial: str) -> dict:
    status = android_usb_status()
    ready = [device for device in status.get("devices", []) if device.get("state") == "device"]
    if not ready:
        code = status.get("state", "no_device")
        raise AndroidDeviceError(code, status.get("message", "没有检测到已授权的安卓手机"))
    target = next((device for device in ready if device["serial"] == serial), None) if serial else ready[0]
    if target is None:
        raise AndroidDeviceError("no_device", "刚才选择的安卓手机已经断开，请重新检测")
    target_serial = target["serial"]
    properties = _parse_getprop(_shell(target_serial, "getprop", 12))
    df_output = _shell(target_serial, "df -k /data", 8)
    battery_text = _shell(target_serial, "dumpsys battery", 8)
    sysfs_command = (
        "for f in /sys/class/power_supply/battery/soh /sys/class/power_supply/Battery/soh "
        "/sys/class/power_supply/battery/cycle_count /sys/class/power_supply/Battery/cycle_count "
        "/sys/class/power_supply/battery/charge_full /sys/class/power_supply/battery/charge_full_design "
        "/sys/class/power_supply/battery/fcc /sys/class/power_supply/battery/design_capacity; "
        "do if [ -r $f ]; then echo $(basename $f)=$(cat $f); fi; done"
    )
    try:
        sysfs_text = _shell(target_serial, sysfs_command, 8)
    except AndroidDeviceError:
        sysfs_text = ""
    return normalize_android_payload(properties, _storage_total_kb(df_output), battery_text, sysfs_text, target_serial)


def read_android_usb(serial: str | None = None) -> dict:
    if not ANDROID_READ_LOCK.acquire(blocking=False):
        raise AndroidDeviceError("busy", "正在读取另一台安卓手机，请稍等几秒再试")
    try:
        return _read_android(str(serial or "").strip())
    finally:
        ANDROID_READ_LOCK.release()
