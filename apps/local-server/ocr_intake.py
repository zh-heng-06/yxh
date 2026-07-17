from __future__ import annotations

import re
import threading
from pathlib import Path

OCR_LOCK = threading.Lock()
OCR_ENGINE = None


def _engine():
    global OCR_ENGINE
    if OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        OCR_ENGINE = RapidOCR()
    return OCR_ENGINE


def _normal(text: str) -> str:
    return re.sub(r"[\s:：]+", "", str(text)).strip()


def _number(text: str) -> int | None:
    match = re.search(r"\d+", str(text).replace(",", ""))
    return int(match.group()) if match else None


def recognize_device_screenshot(image_path: Path) -> dict:
    with OCR_LOCK:
        result, _ = _engine()(str(image_path))
    if not result:
        raise ValueError("没有从截图中识别到文字")

    rows = [
        {"text": str(item[1]).strip(), "score": float(item[2])}
        for item in result
        if str(item[1]).strip()
    ]
    texts = [row["text"] for row in rows]
    normalized = [_normal(text) for text in texts]

    def after(*labels: str) -> str:
        wanted = {_normal(label) for label in labels}
        for index, text in enumerate(normalized[:-1]):
            if text in wanted:
                return texts[index + 1].strip()
        return ""

    model = after("设备型号", "手机型号", "型号")
    imei = re.sub(r"\D", "", after("IMEI", "IMEI1"))
    imei2 = re.sub(r"\D", "", after("IMEI2"))
    if len(imei) != 15:
        imei = next(
            (digits for text in texts if len((digits := re.sub(r"\D", "", text))) == 15),
            "",
        )
    if imei2 == imei or len(imei2) != 15:
        imei2 = ""

    storage = after("存储容量", "容量", "ROM", "机身存储")
    storage_match = re.search(r"\b(32|64|128|256|512|1024)\s*G(?:B)?\b", storage, re.I)
    storage = f"{storage_match.group(1)}GB" if storage_match else ""

    lower_model = model.lower()
    if "iphone" in lower_model:
        brand = "Apple"
    elif "huawei" in lower_model or "华为" in model:
        brand = "华为"
    elif "honor" in lower_model or "荣耀" in model:
        brand = "荣耀"
    elif "xiaomi" in lower_model or "redmi" in lower_model or "小米" in model or "红米" in model:
        brand = "小米"
    elif "oppo" in lower_model:
        brand = "OPPO"
    elif "vivo" in lower_model or "iqoo" in lower_model:
        brand = "vivo"
    else:
        brand = "其他"

    battery = _number(after("电池寿命", "电池健康", "电池效率"))
    cycles = _number(after("充电次数", "循环次数"))
    system_version = after("固件版本", "系统版本", "iOS版本", "安卓版本", "Android版本")
    color = after("外壳颜色", "机身颜色", "颜色")

    if not model:
        raise ValueError("没有识别到设备型号，请换一张完整截图")
    if len(imei) != 15:
        raise ValueError("没有识别到完整的15位IMEI，请换清晰截图")

    return {
        "brand": brand,
        "model": model,
        "storage": storage,
        "color": color,
        "systemVersion": system_version,
        "batteryHealth": battery,
        "chargeCycles": cycles,
        "conditionGrade": "95新",
        "imei": imei,
        "imei2": imei2,
        "serialNumber": after("序列号", "SN", "Serial Number"),
        "sourceFields": {
            "source": "local_ocr_screenshot",
            "salesModel": after("销售型号"),
            "productType": after("产品类型"),
            "salesRegion": after("销售地区"),
            "screenResolution": after("屏幕分辨率"),
            "ocrText": texts,
        },
        "confidence": round(sum(row["score"] for row in rows) / len(rows), 3),
    }
