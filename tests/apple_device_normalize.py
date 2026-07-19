from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "local-server"))

from apple_device import normalize_apple_payload


def main() -> int:
    payload = normalize_apple_payload(
        {
            "ProductType": "iPhone17,2",
            "HardwareModel": "D94AP",
            "ModelNumber": "MYW33",
            "RegionInfo": "LL/A",
            "ActivationState": "Activated",
            "TrustedHostAttached": True,
            "ProductVersion": "26.5.2",
            "BuildVersion": "23F84",
            "InternationalMobileEquipmentIdentity": "356000000000011",
            "InternationalMobileEquipmentIdentity2": "356000000000029",
            "SerialNumber": "TESTSERIAL1",
        },
        {"TotalDiskCapacity": 256_000_000_000},
        {"DesignCapacity": 4630, "NominalChargeCapacity": 3945, "CycleCount": 308, "Serial": "TESTBATTERY1"},
        "iPhone 16 Pro Max",
        "00008140-TEST000000000001",
    )
    assert payload["model"] == "iPhone 16 Pro Max"
    assert payload["storage"] == "256GB"
    assert payload["batteryHealth"] == 85
    assert payload["chargeCycles"] == 308
    assert payload["imei"] == "356000000000011"
    assert payload["imei2"] == "356000000000029"
    assert payload["serialNumber"] == "TESTSERIAL1"
    assert payload["sourceFields"]["mode"] == "apple_usb"
    assert payload["sourceFields"]["udidTail"] == "00000001"
    assert "WiFiAddress" not in payload["sourceFields"]
    assert payload["color"] == ""
    print("PASS | 苹果直连字段归一化、容量与电池健康计算、隐私字段过滤")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
