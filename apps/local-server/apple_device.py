from __future__ import annotations

import asyncio
import threading
from typing import Any


APPLE_READ_LOCK = threading.Lock()
KNOWN_STORAGE_GB = (16, 32, 64, 128, 256, 512, 1024, 2048)


class AppleDeviceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _load_driver():
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        from pymobiledevice3.usbmux import list_devices
    except ImportError as error:
        raise AppleDeviceError(
            "dependency_missing",
            "苹果直连组件尚未安装，请在电脑运行 python -m pip install -r requirements.txt 后重启系统",
        ) from error
    return create_using_usbmux, DiagnosticsService, list_devices


def _run(coroutine, timeout: float):
    try:
        return asyncio.run(asyncio.wait_for(coroutine, timeout=timeout))
    except TimeoutError as error:
        raise AppleDeviceError("timeout", "读取超时，请保持手机解锁并重新插拔数据线后重试") from error


def _storage_label(total_bytes: Any) -> str:
    try:
        decimal_gb = float(total_bytes) / 1_000_000_000
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    closest = min(KNOWN_STORAGE_GB, key=lambda value: abs(value - decimal_gb))
    if abs(closest - decimal_gb) > max(4, closest * 0.12):
        return ""
    return f"{closest}GB" if closest < 1024 else f"{closest // 1024}TB"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _battery_health(battery: dict) -> int | None:
    design = _safe_int(battery.get("DesignCapacity"))
    nominal = _safe_int(battery.get("NominalChargeCapacity"))
    if not design or not nominal:
        return None
    return max(0, min(100, round(nominal / design * 100)))


def normalize_apple_payload(
    all_values: dict,
    disk_usage: dict | None,
    battery: dict | None,
    display_name: str | None,
    udid: str,
    warnings: list[str] | None = None,
) -> dict:
    """Convert device protocol values into the existing intake contract.

    Only business-relevant hardware facts are retained. Contacts, photos,
    messages, account data and MAC addresses are deliberately not read.
    """
    disk_usage = disk_usage or {}
    battery = battery or {}
    region = str(all_values.get("RegionInfo") or "").strip()
    sales_model = str(all_values.get("ModelNumber") or "").strip()
    activation_state = str(all_values.get("ActivationState") or "").strip()
    trusted = bool(all_values.get("TrustedHostAttached"))
    source_fields = {
        "mode": "apple_usb",
        "provider": "pymobiledevice3",
        "connection": "USB",
        "udidTail": udid[-8:],
        "productType": str(all_values.get("ProductType") or ""),
        "hardwareModel": str(all_values.get("HardwareModel") or ""),
        "salesModel": sales_model,
        "salesRegion": region,
        "activationState": activation_state,
        "trustedHost": trusted,
        "buildVersion": str(all_values.get("BuildVersion") or ""),
        "batteryDesignCapacity": _safe_int(battery.get("DesignCapacity")),
        "batteryNominalCapacity": _safe_int(battery.get("NominalChargeCapacity")),
        "batterySerial": str(battery.get("Serial") or ""),
        "readWarnings": list(warnings or []),
    }
    return {
        "brand": "Apple",
        "model": str(display_name or all_values.get("DeviceName") or all_values.get("ProductType") or "iPhone"),
        "storage": _storage_label(disk_usage.get("TotalDiskCapacity")),
        "color": "",
        "systemVersion": str(all_values.get("ProductVersion") or ""),
        "batteryHealth": _battery_health(battery),
        "chargeCycles": _safe_int(battery.get("CycleCount")),
        "imei": str(all_values.get("InternationalMobileEquipmentIdentity") or ""),
        "imei2": str(all_values.get("InternationalMobileEquipmentIdentity2") or ""),
        "serialNumber": str(all_values.get("SerialNumber") or ""),
        "productType": str(all_values.get("ProductType") or ""),
        "salesModel": sales_model,
        "salesRegion": region,
        "activationState": activation_state,
        "trusted": trusted,
        "udid": udid,
        "warnings": list(warnings or []),
        "sourceFields": source_fields,
        "privacy": "仅读取设备硬件与系统资料，未读取照片、通讯录、消息或账号内容",
    }


async def _usb_devices() -> list:
    _, _, list_devices = _load_driver()
    return [device for device in await list_devices() if getattr(device, "is_usb", False)]


def apple_usb_status() -> dict:
    try:
        devices = _run(_usb_devices(), 6)
    except AppleDeviceError as error:
        return {
            "available": False,
            "state": error.code,
            "message": str(error),
            "devices": [],
        }
    except Exception as error:
        return {
            "available": False,
            "state": "driver_error",
            "message": "苹果设备服务暂不可用，请确认 Apple Mobile Device Service 已启动",
            "detail": type(error).__name__,
            "devices": [],
        }
    if not devices:
        return {
            "available": True,
            "state": "no_device",
            "message": "没有检测到USB连接的iPhone，请解锁手机并重新插拔数据线",
            "devices": [],
        }
    rows = [
        {
            "udid": str(device.serial),
            "label": f"iPhone（设备号尾段 {str(device.serial)[-8:]}）",
            "connectionType": str(device.connection_type),
        }
        for device in devices
    ]
    return {
        "available": True,
        "state": "connected",
        "message": f"检测到{len(rows)}台USB连接的iPhone",
        "devices": rows,
    }


def _friendly_error(error: Exception) -> AppleDeviceError:
    name = type(error).__name__
    if name in {"PasswordRequiredError", "PasscodeRequiredError"}:
        return AppleDeviceError("locked", "请解锁iPhone并保持屏幕亮着，然后重新读取")
    if name in {
        "NotTrustedError",
        "NotPairedError",
        "PairingDialogResponsePendingError",
        "InvalidHostIDError",
    }:
        return AppleDeviceError("trust_required", "请解锁iPhone，点按“信任此电脑”并输入锁屏密码，然后重新读取")
    if name == "UserDeniedPairingError":
        return AppleDeviceError("trust_denied", "iPhone拒绝了本次信任，请重新插拔后选择“信任”")
    if name in {"NoDeviceConnectedError", "DeviceNotFoundError", "BadDevError"}:
        return AppleDeviceError("no_device", "读取过程中手机断开，请重新插好数据线")
    if name in {"ConnectionFailedError", "ConnectionFailedToUsbmuxdError"}:
        return AppleDeviceError("driver_error", "无法连接苹果设备服务，请重启电脑后再试")
    return AppleDeviceError("read_failed", f"没有读完手机资料（{name}），请重新插拔后再试")


async def _read_apple_usb(udid: str | None) -> dict:
    create_using_usbmux, DiagnosticsService, _ = _load_driver()
    devices = await _usb_devices()
    if not devices:
        raise AppleDeviceError("no_device", "没有检测到USB连接的iPhone")
    if udid:
        target = next((device for device in devices if str(device.serial) == udid), None)
        if target is None:
            raise AppleDeviceError("no_device", "刚才选择的iPhone已经断开，请重新检测")
    else:
        target = devices[0]

    warnings: list[str] = []
    try:
        async with await create_using_usbmux(
            serial=str(target.serial),
            connection_type="USB",
            pair_timeout=20,
            label="ZhangGuiTai",
        ) as lockdown:
            all_values = dict(lockdown.all_values or {})
            disk_usage: dict = {}
            battery: dict = {}
            try:
                disk_usage = await lockdown.get_value(domain="com.apple.disk_usage") or {}
            except Exception:
                warnings.append("容量未读取，请人工确认")
            try:
                battery = await DiagnosticsService(lockdown=lockdown).get_battery() or {}
            except Exception:
                warnings.append("电池健康和充电次数未读取，请人工确认")
            return normalize_apple_payload(
                all_values,
                disk_usage,
                battery,
                lockdown.display_name,
                str(target.serial),
                warnings,
            )
    except AppleDeviceError:
        raise
    except Exception as error:
        raise _friendly_error(error) from error


def read_apple_usb(udid: str | None = None) -> dict:
    if not APPLE_READ_LOCK.acquire(blocking=False):
        raise AppleDeviceError("busy", "正在读取另一台iPhone，请稍等几秒再试")
    try:
        return _run(_read_apple_usb(udid), 35)
    finally:
        APPLE_READ_LOCK.release()
