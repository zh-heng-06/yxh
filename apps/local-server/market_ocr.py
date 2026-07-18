from __future__ import annotations

import re
from pathlib import Path

from ocr_intake import OCR_LOCK, _engine


CONDITION_COLUMNS = (
    (240, 315, "靓机"),
    (315, 387, "小花"),
    (387, 459, "大花"),
    (459, 532, "外爆"),
    (532, 610, "内爆可测"),
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).strip()


def _token(item) -> dict:
    box, text, score = item
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    return {
        "text": str(text).strip(),
        "clean": _clean(text),
        "score": float(score),
        "x": (min(xs) + max(xs)) / 2,
        "y": (min(ys) + max(ys)) / 2,
        "x0": min(xs),
        "x1": max(xs),
        "y0": min(ys),
        "y1": max(ys),
    }


def _capacity(text: str) -> str:
    value = text.upper().replace("丨", "1").replace("I", "1")
    match = re.fullmatch(r"(64|128|256|512|1T)", value)
    return match.group(1) if match else ""


def _model_name(text: str) -> str:
    value = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    value = re.sub(r"\s+", " ", value)
    if not re.search(r"(?:\d|XS|XR|SE|X$)", value):
        return ""
    if value in {"128", "256", "512", "64", "1T"} or re.fullmatch(r"A\d{4}", value):
        return ""
    replacements = {
        "PROMAX": "PRO MAX",
        "PRO MAX": "PRO MAX",
        "PLUS": "PLUS",
        "MINI": "MINI",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return f"iPhone {value}".replace("  ", " ").strip()


def recognize_market_sheet(image_path: Path) -> dict:
    import cv2

    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError("报价表图片无法读取")
    height, width = image.shape[:2]
    if width < 500 or height < 500:
        raise ValueError("报价表图片尺寸太小，请使用完整原图")

    # 超长表格直接整图识别会被OCR缩得过小。只保留报价列，按约900像素
    # 分段并放大两倍识别，再把坐标还原到原图。
    tokens = []
    crop_width = min(width, 625)
    chunk_height, step = 920, 820
    with OCR_LOCK:
        engine = _engine()
        for top in range(0, height, step):
            bottom = min(height, top + chunk_height)
            chunk = image[top:bottom, :crop_width]
            enlarged = cv2.resize(chunk, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            result, _ = engine(enlarged)
            for item in result or []:
                token = _token(item)
                token.update({
                    "x": token["x"] / 2,
                    "y": token["y"] / 2 + top,
                    "x0": token["x0"] / 2,
                    "x1": token["x1"] / 2,
                    "y0": token["y0"] / 2 + top,
                    "y1": token["y1"] / 2 + top,
                })
                tokens.append(token)
            if bottom == height:
                break
    if not tokens:
        raise ValueError("没有从报价表中识别到文字")
    tokens = [token for token in tokens if token["clean"] and token["score"] >= 0.35]
    title_text = " ".join(token["text"] for token in tokens[:80])
    date_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", title_text)
    captured_on = "-".join((date_match.group(1), date_match.group(2).zfill(2), date_match.group(3).zfill(2))) if date_match else ""

    capacity_rows = []
    for token in tokens:
        capacity = _capacity(token["clean"])
        if 165 <= token["x"] <= 235 and capacity:
            capacity_rows.append({**token, "capacity": "1024GB" if capacity == "1T" else f"{capacity}GB"})

    model_tokens = []
    network_tokens = []
    left_tokens = sorted((token for token in tokens if token["x"] < 105), key=lambda token: token["y"])
    combined_left = list(left_tokens)
    for index, token in enumerate(left_tokens[:-1]):
        following = left_tokens[index + 1]
        if 3 <= following["y"] - token["y"] <= 48:
            combined_left.append({**token, "text":f"{token['text']} {following['text']}", "clean":_clean(f"{token['text']} {following['text']}"), "y":(token["y"]+following["y"])/2, "score":(token["score"]+following["score"])/2})
    for token in tokens:
        if 85 <= token["x"] <= 165:
            match = re.search(r"A\d{4}", token["clean"].upper())
            if match:
                network_tokens.append({**token, "networkModel": match.group()})
    for token in combined_left:
        if token["x"] < 105:
            model = _model_name(token["text"])
            if model:
                model_tokens.append({**token, "model": model})

    def nearest(candidates: list[dict], y: float, limit: float = 230) -> dict | None:
        if not candidates:
            return None
        candidate = min(candidates, key=lambda item: abs(item["y"] - y))
        return candidate if abs(candidate["y"] - y) <= limit else None

    network_model_map = {}
    for network in network_tokens:
        nearby = [model for model in model_tokens if abs(model["y"] - network["y"]) <= 85]
        if nearby:
            # 合并单元格内型号可能被OCR拆成“16 PRO”和“MAX”，优先选择
            # 同一区域中文字更完整的候选，再把该网络型号下的容量统一。
            network_model_map[network["networkModel"]] = max(nearby, key=lambda item: (len(item["model"]), item["score"]))

    rows = []
    for capacity in capacity_rows:
        network = nearest(network_tokens, capacity["y"])
        model = network_model_map.get(network["networkModel"]) if network else nearest(model_tokens, capacity["y"])
        if not model:
            continue
        prices = {}
        price_scores = []
        for token in tokens:
            if abs(token["y"] - capacity["y"]) > 18:
                continue
            digits = re.sub(r"\D", "", token["clean"].replace(",", ""))
            if not 2 <= len(digits) <= 5:
                continue
            for left, right, condition in CONDITION_COLUMNS:
                if left <= token["x"] < right:
                    price = int(digits)
                    if 50 <= price <= 50000:
                        prices[condition] = price
                        price_scores.append(token["score"])
                    break
        if len(prices) < 2:
            continue
        rows.append({
            "brand": "Apple",
            "model": model["model"],
            "networkModel": network["networkModel"] if network else "",
            "storage": capacity["capacity"],
            "prices": prices,
            "confidence": round(sum(price_scores + [capacity["score"], model["score"]]) / (len(price_scores) + 2), 3),
        })

    unique = {}
    for row in rows:
        key = (row["model"].lower(), row["storage"])
        if key not in unique or row["confidence"] > unique[key]["confidence"]:
            unique[key] = row
    rows = sorted(unique.values(), key=lambda row: next((token["y"] for token in capacity_rows if token["capacity"] == row["storage"] and nearest(model_tokens, token["y"]) and nearest(model_tokens, token["y"])["model"] == row["model"]), 0))
    if not rows:
        raise ValueError("没有识别到报价行，请确认图片是完整报价表原图")
    return {
        "capturedOn": captured_on,
        "rows": rows,
        "rowCount": len(rows),
        "quoteCount": sum(len(row["prices"]) for row in rows),
        "ocrTokenCount": len(tokens),
        "mode": "local-table-ocr",
    }
