from __future__ import annotations

import argparse
import json
from pathlib import Path

from integration_smoke import Client, check, image_data_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:4197")
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    owner = Client(args.base)
    if not owner.call("/api/setup-status")["configured"]:
        owner.call("/api/setup", "POST", {"shopName":"客户流程测试店","username":"owner","displayName":"老板","password":"test123456"})
    owner.call("/api/login", "POST", {"username":"owner","password":"test123456"})
    image = image_data_url(args.image)

    full = owner.call("/api/devices/intake", "POST", {"brand":"Apple","model":"iPhone 16 Pro","storage":"256GB","imei":"359000000000001","purchaseCost":5000,"listPrice":5600}, expected=201)
    detail = owner.call(f"/api/devices/{full['id']}")
    check(detail["intake_state"] == "complete" and detail["appearance_status"] == "pending", "完整入库立即可售、外观待拍")
    owner.call(f"/api/devices/{full['id']}/photos", "POST", {"photoType":"back","image":image}, expected=201)
    owner.call(f"/api/devices/{full['id']}/appearance/confirm", "POST", {"result":"no_obvious_defect"}, expected=409)
    owner.call(f"/api/devices/{full['id']}/photos", "POST", {"photoType":"front","image":image}, expected=201)
    confirmed = owner.call(f"/api/devices/{full['id']}/appearance/confirm", "POST", {"result":"no_obvious_defect"})
    check(confirmed["appearanceStatus"] == "complete_no_defect", "正背面外观确认与缺图阻止确认")

    quick = owner.call("/api/devices/quick-intake", "POST", {"model":"Mate 70 Pro","storage":"512GB","imei":"359000000000002","purchaseCost":4200,"brand":"华为"}, expected=201)
    owner.call(f"/api/devices/{quick['id']}/sell", "POST", {"salePrice":5000}, expected=409)
    owner.call(f"/api/devices/{quick['id']}/update", "POST", {"brand":"华为","model":"Mate 70 Pro","storage":"512GB","imei":"359000000000002","listPrice":5000,"conditionGrade":"95新","markComplete":True})
    check(owner.call(f"/api/devices/{quick['id']}")["intake_state"] == "complete", "快速入库在手机同接口补全后可售")

    sale = owner.call(f"/api/devices/{full['id']}/sell", "POST", {"salePrice":5500,"paymentMethod":"微信","customerQuickNote":"预算六千，想要苹果，关注电池，明天联系"})
    tasks = owner.call("/api/customer-tasks")
    check(len(tasks) == 1 and tasks[0]["id"] == sale["customerTaskId"], "每次出库原子生成一条客户待办")
    owner.call(f"/api/devices/{full['id']}/sell", "POST", {"salePrice":5500}, expected=409)
    check(len(owner.call("/api/customer-tasks")) == 1, "重复出库不重复创建客户待办")

    parsed = owner.call("/api/customer-notes/parse", "POST", {"text":"预算六千，想要苹果，关注电池，明天联系；15岁学生"})
    check(parsed["requiresConfirmation"] and parsed["suggestions"]["budgetMax"] == 6000 and "电池" in parsed["suggestions"]["concerns"] and parsed["suggestions"]["nextFollowupAt"] and parsed["warnings"], "本地速记建议、跟进日期与敏感信息提醒")
    owner.call(f"/api/customer-interactions/{sale['customerTaskId']}/complete", "POST", {"sourceChannel":"到店","budgetMax":6000,"preferredBrand":"Apple","concerns":["电池"],"outcome":"purchased","confirmedSummary":"预算6000，关注电池"})
    check(not owner.call("/api/customer-tasks"), "匿名成交记录可补齐")

    first = owner.call("/api/customer-interactions", "POST", {"interactionType":"inquiry","rawNote":"想看华为","displayName":"测试客户","phone":"13800138000","consent":True,"complete":True,"preferredModel":"Mate 70"}, expected=201)
    second = owner.call("/api/customer-interactions", "POST", {"interactionType":"repair","rawNote":"返店咨询","displayName":"同一客户","phone":"13800138000","consent":True,"complete":True}, expected=201)
    check(first["customerId"] == second["customerId"] and len(owner.call("/api/customers")) == 1, "手机号标准化去重并合并互动")
    owner.call("/api/customer-interactions", "POST", {"interactionType":"inquiry","displayName":"未同意客户","phone":"13900000000","complete":True}, expected=400)
    check(True, "未同意时拒绝保存可识别资料")

    name_only_a = owner.call("/api/customer-interactions", "POST", {"interactionType":"inquiry","displayName":"待合并甲","consent":True,"complete":True}, expected=201)
    name_only_b = owner.call("/api/customer-interactions", "POST", {"interactionType":"trade_in","displayName":"待合并乙","consent":True,"complete":True}, expected=201)
    owner.call(f"/api/customers/{name_only_a['customerId']}/update", "POST", {"displayName":"待合并甲","phone":"13700137000","consent":True})
    merged = owner.call(f"/api/customers/{name_only_b['customerId']}/update", "POST", {"displayName":"待合并乙","phone":"13700137000","consent":True})
    merged_detail = owner.call(f"/api/customers/{name_only_a['customerId']}")
    check(merged["mergedInto"] == name_only_a["customerId"] and len(merged_detail["interactions"]) == 2, "补录相同手机号时合并重复客户档案")

    owner.call("/api/users", "POST", {"username":"staff","displayName":"店员","password":"staff12345","role":"staff"}, expected=201)
    staff = Client(args.base); staff.call("/api/login", "POST", {"username":"staff","password":"staff12345"})
    masked = staff.call("/api/customers")[0]
    check("••••" in masked["phone"] and "13800138000" not in json.dumps(masked, ensure_ascii=False), "店员客户列表默认遮挡手机号")
    revealed = staff.call(f"/api/customers/{first['customerId']}/reveal-contact", "POST", {})
    check(revealed["phone"] == "13800138000", "单个客户查看完整号码")
    audits = owner.call("/api/audit-events?action=customer_contact_reveal")
    check(len(audits) == 1 and "13800138000" not in json.dumps(audits, ensure_ascii=False), "查看号码写日志且日志不含号码")

    content = owner.call("/api/customer-insights?mode=content")
    check(content["privacy"]["containsPersonalInformation"] is False and not content["models"], "公开知识汇总去身份化且少于5条不输出")
    owner.call(f"/api/customers/{first['customerId']}/anonymize", "POST", {})
    anonymized = owner.call(f"/api/customers/{first['customerId']}")
    check(anonymized["status"] == "anonymized" and not anonymized["phone"] and not anonymized["display_name"] and all(not row["raw_note"] for row in anonymized["interactions"]), "两年复核后的确认匿名化清除身份和原始文字")
    owner.call("/api/export/customers.csv", expected=404, raw=True)
    check(True, "客户资料无批量导出接口")

    dashboard = owner.call("/api/dashboard")
    check("pendingPhotosCount" in dashboard and "pendingCustomerCount" in dashboard and "followupDueCount" in dashboard, "首页外观、客户待补和跟进提醒")
    print("CUSTOMER_WORKFLOW_SMOKE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
