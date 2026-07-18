const $ = selector => document.querySelector(selector);
const escText = value => value === null || value === undefined || value === "" ? "-" : String(value);
const money = value => `¥${Number(value || 0).toLocaleString("zh-CN",{maximumFractionDigits:2})}`;
const dateText = value => value ? new Date(value).toLocaleString("zh-CN") : "-";
const row = (label,value) => { const box=document.createElement("div"),dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=escText(value);box.append(dt,dd);return box; };

async function boot(){
  const token=new URLSearchParams(location.search).get("t")||"";
  if(!token)throw new Error("交接卡链接不完整");
  const response=await fetch(`/api/public/handoffs/${encodeURIComponent(token)}`,{cache:"no-store"});
  const data=await response.json().catch(()=>({error:"系统无法读取交接卡"}));
  if(!response.ok)throw new Error(data.error||"交接卡无法打开");
  const device=data.device,sale=data.sale,warranty=data.warranty;
  document.title=`${data.shopName}·放心交接卡`;
  $("#shop-name").textContent=data.shopName;$("#handoff-number").textContent=`交接编号 ${data.handoffNumber}`;
  [["设备",`${device.model} ${device.storage}`],["颜色",device.color],["成色",device.conditionGrade],["库存编号",device.stockCode],["IMEI尾号",device.imeiTail],["电池健康",device.batteryHealth===null?"-":`${device.batteryHealth}%`],["充电次数",device.chargeCycles===null?"-":`${device.chargeCycles}次`],["系统版本",device.systemVersion]].forEach(item=>$("#device-info").append(row(...item)));
  [["成交价",money(sale.price)],["支付方式",sale.paymentMethod],["赠品",sale.gifts.length?sale.gifts.join("、"):"无"],["成交时间",dateText(data.createdAt)]].forEach(item=>$("#sale-info").append(row(...item)));
  const badge=$("#warranty-badge");badge.classList.add(warranty.status);
  badge.textContent=warranty.status==="none"?"本单未提供门店质保":`门店质保 ${warranty.days}天 · 至 ${(warranty.expiresAt||"").slice(0,10)}${warranty.status==="expired"?" · 已过期":""}`;
  $("#warranty-terms").textContent=warranty.terms;$("#disclosure").textContent=data.disclosure||"未额外记录已知情况";$("#unchecked").textContent=data.unchecked||"未额外记录未检测项";
  const checklist=$("#checklist");(data.checklist.length?data.checklist:["未记录交机确认项"]).forEach(value=>{const item=document.createElement("li");item.textContent=value;if(!data.checklist.length)item.className="empty";checklist.append(item);});
  $("#notice").textContent=data.notice;$("#save-card").href=data.cardUrl;$("#loading").hidden=true;$("#handoff-card").hidden=false;
}

boot().catch(error=>{$("#loading").hidden=true;$("#error-message").textContent=error.message;$("#error").hidden=false;});
