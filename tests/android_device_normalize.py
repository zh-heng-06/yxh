from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "local-server"))

from android_device import _parse_devices, normalize_android_payload


def main() -> int:
    devices = _parse_devices(
        "List of devices attached\nTEST-HUAWEI-0001 device product:ALT-LX9 model:ALT_LX9 device:ALT transport_id:1\n"
        "TEST-XIAOMI-0002 unauthorized usb:1-2 transport_id:2\n"
    )
    assert len(devices) == 2
    assert devices[0]["state"] == "device"
    assert devices[0]["model"] == "ALT LX9"
    assert devices[1]["state"] == "unauthorized"

    huawei = normalize_android_payload(
        {
            "ro.product.manufacturer": "HUAWEI",
            "ro.product.brand": "HUAWEI",
            "ro.config.marketing_name": "HUAWEI Mate 60 Pro",
            "ro.product.model": "ALN-AL00",
            "hw_sc.build.platform.version": "4.2.0",
            "ro.build.version.release": "12",
            "ro.build.version.sdk": "31",
            "ro.serialno": "TESTSERIALHUAWEI",
            "persist.radio.imei": "356000000000101,356000000000119",
        },
        238_000_000,
        "level: 82\nhealth: 2",
        "soh=91\ncycle_count=267\ncharge_full=4300000\ncharge_full_design=4700000",
        "TEST-HUAWEI-0001",
    )
    assert huawei["brand"] == "华为"
    assert huawei["model"] == "HUAWEI Mate 60 Pro"
    assert huawei["storage"] == "256GB"
    assert huawei["systemVersion"] == "HarmonyOS 4.2.0"
    assert huawei["batteryHealth"] == 91
    assert huawei["chargeCycles"] == 267
    assert huawei["imei"] == "356000000000101"
    assert huawei["imei2"] == "356000000000119"
    assert huawei["sourceFields"]["mode"] == "android_usb"
    assert huawei["sourceFields"]["serialTail"] == "WEI-0001"
    assert "photos" not in huawei["sourceFields"]

    xiaomi = normalize_android_payload(
        {
            "ro.product.manufacturer": "Xiaomi",
            "ro.product.brand": "Redmi",
            "ro.product.marketname": "REDMI K80",
            "ro.build.version.release": "15",
            "ro.mi.os.version.name": "OS2.0",
        },
        474_000_000,
        "level: 78",
        "",
        "TEST-XIAOMI-0002",
    )
    assert xiaomi["brand"] == "小米"
    assert xiaomi["storage"] == "512GB"
    assert xiaomi["systemVersion"] == "HyperOS OS2.0"
    assert xiaomi["imei"] == ""
    assert len(xiaomi["warnings"]) >= 3
    print("PASS | 安卓/华为直连字段归一化、容量、电池、系统版本、权限空白与隐私过滤")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
