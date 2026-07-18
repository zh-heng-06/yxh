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
    value = text.upper().replace("丨", "1").replace("I", "1").replace(" ", "")
    value = re.sub(r"(?:GB|G)$", "", value)
    value = value.replace("1TB", "1T")
    match = re.fullmatch(r"(16|32|64|128|256|512|1T|2T)", value)
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


def _line_positions(values, threshold: int) -> list[int]:
    import numpy as np

    positions = np.where(values >= threshold)[0]
    groups: list[list[int]] = []
    for value in positions:
        value = int(value)
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [round(sum(group) / len(group)) for group in groups]


def _dedupe_tokens(tokens: list[dict]) -> list[dict]:
    result: list[dict] = []
    for token in sorted(tokens, key=lambda item: (item["y"], item["x"], -item["score"])):
        duplicate = next((item for item in result if item["clean"] == token["clean"] and abs(item["x"] - token["x"]) < 6 and abs(item["y"] - token["y"]) < 8), None)
        if duplicate:
            if token["score"] > duplicate["score"]:
                duplicate.update(token)
        else:
            result.append(token)
    return result


def _ocr_crop(engine, crop, scale: float, offset_x: float, offset_y: float) -> list[dict]:
    import cv2

    if crop.size == 0:
        return []
    enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    result, _ = engine(enlarged)
    tokens = []
    for item in result or []:
        token = _token(item)
        for key in ("x", "y", "x0", "x1", "y0", "y1"):
            token[key] /= scale
        token["x"] += offset_x
        token["x0"] += offset_x
        token["x1"] += offset_x
        token["y"] += offset_y
        token["y0"] += offset_y
        token["y1"] += offset_y
        tokens.append(token)
    return tokens


def _rows_from_tokens(tokens: list[dict], model: str, network_model: str, capacity_bounds: tuple[int, int], condition_columns: tuple[tuple[int, int, str], ...]) -> list[dict]:
    rows = []
    capacities = []
    for token in tokens:
        capacity = _capacity(token["clean"])
        if capacity_bounds[0] <= token["x"] <= capacity_bounds[1] and capacity:
            capacities.append((token, {"1T":"1024GB","2T":"2048GB"}.get(capacity, f"{capacity}GB")))
    for token, capacity in capacities:
        prices = {}
        scores = []
        for candidate in tokens:
            if abs(candidate["y"] - token["y"]) > 16:
                continue
            digits = re.sub(r"\D", "", candidate["clean"].replace(",", ""))
            if not 2 <= len(digits) <= 5:
                continue
            for left, right, condition in condition_columns:
                if left <= candidate["x"] < right:
                    price = int(digits)
                    if 50 <= price <= 50000:
                        prices[condition] = price
                        scores.append(candidate["score"])
                    break
        rows.append({
            "brand": "Apple",
            "model": model,
            "networkModel": network_model,
            "storage": capacity,
            "prices": prices,
            "confidence": round(sum(scores + [token["score"]]) / (len(scores) + 1), 3),
            "sourceY": round(token["y"]),
        })
    return rows


def recognize_market_sheet(image_path: Path) -> dict:
    import cv2
    import numpy as np

    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError("报价表图片无法读取")
    height, width = image.shape[:2]
    if width < 500 or height < 500:
        raise ValueError("报价表图片尺寸太小，请使用完整原图")

    # 此报价单是规则表格。旧版按固定高度切长图会在重叠边界漏掉中间容量行。
    # 先把宽度归一化，再用真实表格线确定“一个型号”和“一个容量”应有的行数；
    # OCR只处理对应单元格。识别数量与表格线数量不一致时明确判为不完整。
    if width != 857:
        ratio = 857 / width
        image = cv2.resize(image, (857, round(height * ratio)), interpolation=cv2.INTER_CUBIC)
        height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dark = gray < 115
    vertical_lines = _line_positions(dark.sum(axis=0), round(height * .65))
    if len(vertical_lines) < 11:
        raise ValueError("报价表竖向单元格不完整，请使用网页中的完整原图")
    # 有保表在五个普通档位前多一个“高保充新”列；无保表为五档。
    condition_names = ("高保充新","靓机","小花","大花","外爆","内爆可测") if len(vertical_lines) >= 12 else ("靓机","小花","大花","外爆","内爆可测")
    model_right, network_right, capacity_right = vertical_lines[1:4]
    capacity_bounds = (network_right, capacity_right)
    condition_boundaries = vertical_lines[3:4 + len(condition_names)]
    condition_columns = tuple((condition_boundaries[index],condition_boundaries[index + 1],name) for index,name in enumerate(condition_names))
    quote_right = condition_boundaries[-1]
    row_lines = _line_positions(dark[:, network_right - 5:quote_right + 5].sum(axis=1), round((quote_right - network_right) * .62))
    full_lines = _line_positions(dark[:, :network_right].sum(axis=1), round(network_right * .78))
    if len(row_lines) < 5 or len(full_lines) < 3:
        raise ValueError("报价表格线不完整，请使用网页中的完整原图")

    left_tokens: list[dict] = []
    with OCR_LOCK:
        engine = _engine()
        chunk_height, step = 920, 820
        for top in range(0, height, step):
            bottom = min(height, top + chunk_height)
            left_tokens.extend(_ocr_crop(engine, image[top:bottom, :network_right + 8], 3, 0, top))
            if bottom == height:
                break
        left_tokens = _dedupe_tokens([token for token in left_tokens if token["clean"] and token["score"] >= 0.35])
    model_tokens = []
    network_tokens = []
    model_side = sorted((token for token in left_tokens if token["x"] < model_right + 8), key=lambda token: token["y"])
    combined_left = list(model_side)
    for index, token in enumerate(model_side[:-1]):
        following = model_side[index + 1]
        if 3 <= following["y"] - token["y"] <= 48:
            combined_left.append({**token, "text":f"{token['text']} {following['text']}", "clean":_clean(f"{token['text']} {following['text']}"), "y":(token["y"]+following["y"])/2, "score":(token["score"]+following["score"])/2})
    for token in left_tokens:
        if model_right - 3 <= token["x"] <= network_right + 3:
            match = re.search(r"A\d{4}", token["clean"].upper())
            if match:
                network_tokens.append({**token, "networkModel": match.group()})
    for token in combined_left:
        if token["x"] < model_right + 8:
            model = _model_name(token["text"])
            if model:
                model_tokens.append({**token, "model": model})

    model_units = []
    for network in network_tokens:
        nearby = [model for model in model_tokens if abs(model["y"] - network["y"]) <= 90]
        if nearby:
            model = max(nearby, key=lambda item: (len(item["model"]), item["score"]))
            model_units.append({"model":model["model"],"networkModel":network["networkModel"],"y":network["y"],"score":(model["score"]+network["score"])/2})
    model_units = sorted(model_units, key=lambda item: item["y"])
    if not model_units:
        raise ValueError("没有识别到型号与网络型号")

    rows = []
    incomplete_blocks = []
    mandatory_conditions = set(condition_names) - {"高保充新"}
    with OCR_LOCK:
        engine = _engine()
        for start, end in zip(full_lines, full_lines[1:]):
            boundaries = [value for value in row_lines if start <= value <= end]
            if not boundaries or boundaries[0] != start:
                boundaries.insert(0,start)
            if boundaries[-1] != end:
                boundaries.append(end)
            if len(boundaries) < 2:
                continue
            block_tokens = _ocr_crop(engine, image[start + 1:end, network_right:quote_right], 2, network_right, start + 1)
            block_rows = _rows_from_tokens(block_tokens, "", "", capacity_bounds, condition_columns)
            for row_top, row_bottom in zip(boundaries, boundaries[1:]):
                if row_bottom - row_top < 20 or row_bottom - row_top > 140:
                    continue
                candidates = [row for row in block_rows if row_top < row["sourceY"] < row_bottom]
                if not candidates or any(not mandatory_conditions.issubset(row["prices"]) for row in candidates):
                    retry_tokens = _ocr_crop(engine, image[row_top + 1:row_bottom, network_right:quote_right], 3, network_right, row_top + 1)
                    retry_rows = _rows_from_tokens(retry_tokens, "", "", capacity_bounds, condition_columns)
                    for retry in retry_rows:
                        old = next((index for index,row in enumerate(candidates) if row["storage"] == retry["storage"]), None)
                        if old is None:
                            candidates.append(retry)
                        elif (len(retry["prices"]),retry["confidence"]) > (len(candidates[old]["prices"]),candidates[old]["confidence"]):
                            candidates[old] = retry
                    if not candidates:
                        numeric_prices = 0
                        for token in retry_tokens:
                            digits = re.sub(r"\D", "", token["clean"])
                            if 2 <= len(digits) <= 5 and any(left <= token["x"] < right for left,right,_ in condition_columns):
                                numeric_prices += 1
                        if numeric_prices >= 3:
                            incomplete_blocks.append({"rowTop":row_top,"rowBottom":row_bottom,"reason":"有价格但容量未识别"})
                for candidate in candidates:
                    unit = min(model_units, key=lambda item: abs(item["y"] - candidate["sourceY"]))
                    if abs(unit["y"] - candidate["sourceY"]) > 260:
                        incomplete_blocks.append({"storage":candidate["storage"],"rowTop":row_top,"reason":"容量未匹配到型号"})
                        continue
                    candidate["model"] = unit["model"]
                    candidate["networkModel"] = unit["networkModel"]
                    rows.append(candidate)

    raw_row_count = len(rows)
    unique = {}
    for row in sorted(rows, key=lambda item: item["sourceY"]):
        key = (row["model"].lower(), row["storage"])
        if key not in unique or (len(row["prices"]), row["confidence"]) > (len(unique[key]["prices"]), unique[key]["confidence"]):
            unique[key] = row
    rows = sorted(unique.values(), key=lambda row: row["sourceY"])
    for row in rows:
        row.pop("sourceY", None)
    if not rows:
        raise ValueError("没有识别到报价行，请确认图片是完整报价表原图")
    expected_rows = raw_row_count + len(incomplete_blocks)
    incomplete_price_rows = [row for row in rows if not mandatory_conditions.issubset(row["prices"])]
    missing_rows = max(0, expected_rows - len(rows))
    complete = missing_rows == 0 and not incomplete_blocks and not incomplete_price_rows
    return {
        "capturedOn": "",
        "rows": rows,
        "rowCount": len(rows),
        "expectedRowCount": expected_rows,
        "missingRowCount": missing_rows,
        "quoteCount": sum(len(row["prices"]) for row in rows),
        "conditions": list(condition_names),
        "complete": complete,
        "incompleteRows": (incomplete_blocks + [{"model":row["model"],"storage":row["storage"]} for row in incomplete_price_rows])[:30],
        "ocrTokenCount": len(left_tokens),
        "mode": "local-grid-cell-ocr",
    }
