import { LocalClient } from "./src/local-client.js?v=27";

const api = new LocalClient();
const $ = selector => document.querySelector(selector);
const money = value => `¥${Number(value || 0).toLocaleString("zh-CN")}`;
const roles = { owner: "老板", staff: "店员" };
const statusNames = {
  in_stock: "在库", reserved: "已预订", sold_pending_pickup: "已售待取",
  sold: "已售", in_repair: "送修中", borrowed_for_test: "借出测试",
  peer_transfer: "同行调拨", return_processing: "退货处理中", scrapped: "报废"
};
const scopeNames = {today_intake:"今日入库",today_sold:"今日出库 / 已售",reserved:"预订中",pending_pickup:"已售待取",in_stock:"在库手机",unprinted:"待打印标签",aged:"超30天库存",all:"全部库存"};
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
}[char]));

let user = null;
let devices = [];
let listScope = "today_intake";
let recognizedScreenshot = null;
let currentDetail = null;
let scanCandidate = null;
let scanStream = null;
let scanLoopToken = 0;
let scanBusy = false;
let scanLastFrame = 0;
let scanDetector = null;
let ledgerRows = [];
let marketEvidence = null;
let marketSheetResult = null;
let toastTimer;
let searchTimer;

function show(id) {
  ["setup", "login", "app"].forEach(name => $(`#${name}`).hidden = name !== id);
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 2200);
}

async function boot() {
  const state = await api.setupStatus();
  if (!state.configured) return show("setup");
  try {
    user = await api.me();
    await loadApp();
  } catch {
    show("login");
  }
}

async function loadApp() {
  show("app");
  user = user || await api.me();
  $("#shop-name").textContent = user.shop_name;
  $("#role-text").textContent = `${user.display_name} · ${roles[user.role]}`;
  $("#market-button").hidden = user.role !== "owner";
  await Promise.all([refresh(), refreshSystemStatus()]);
}

async function refreshSystemStatus() {
  const status = await api.status();
  $("#connection-text").textContent = status.database === "ok" ? "SQLite共享数据库正常" : "数据库需要检查";
  $("#printer-text").textContent = `打印机：${status.printer.connected ? "NIIMBOT B1 已连接" : status.printer.status}`;
  $("#system-status").textContent = `数据库：${status.database}；打印机：${status.printer.status}；手机地址：${status.lanUrl}`;
  const backup = status.backup;
  if (backup) {
    const last = backup.lastBackupAt ? new Date(backup.lastBackupAt).toLocaleString("zh-CN") : "尚无备份";
    $("#backup-policy").textContent = `${backup.schedule}自动备份；最近一次：${last}；校验：${backup.verified ? "通过" : "需要检查"}`;
    $("#backup-policy").className = backup.verified ? "backup-ok" : "backup-warning";
  }
}

async function renderUsers() {
  if (user.role !== "owner") return;
  const users = await api.users();
  $("#user-list").innerHTML = users.map(item => `<div class="user-row"><span>${esc(item.display_name)}（${esc(item.username)}）</span><span>${roles[item.role]} · ${item.active ? "启用" : "停用"} ${item.id!==user.id?`<button type="button" data-user-toggle="${item.id}">${item.active?"停用":"启用"}</button>`:""}</span></div>`).join("");
}

async function renderBackups() {
  if (user.role !== "owner") { $("#backup-list").innerHTML = ""; return; }
  const backups = await api.backups();
  $("#backup-list").innerHTML = backups.slice(0, 8).map(item => `<div class="user-row"><span>${esc(item.name)} · ${(item.size / 1024).toFixed(0)}KB</span><button type="button" data-restore="${esc(item.name)}">恢复</button></div>`).join("");
}

async function refresh() {
  const search = $("#search").value.trim();
  const [dash, list] = await Promise.all([
    api.dashboard(),
    api.devices(search, search ? "all" : listScope)
  ]);
  devices = list;
  $("#list-title").textContent = search ? "搜索结果" : (scopeNames[listScope] || "手机库存");
  $("#active-count").textContent = `${dash.activeCount} 台`;
  $("#aged-count").textContent = dash.agedCount;
  $("#today-intake").textContent = dash.todayIntake;
  $("#today-sold").textContent = dash.todaySold;
  $("#reserved-count").textContent = dash.reservedCount;
  $("#pending-pickup-count").textContent = dash.pendingPickupCount;
  $("#unprinted-count").textContent = dash.unprintedCount;
  $("#inventory-cost").textContent = user.role === "owner" ? `库存成本 ${money(dash.inventoryCost)}` : "成本仅老板可见";
  $("#today-profit").textContent = user.role === "owner" ? `今日毛利 ${money(dash.todayProfit)}` : "今日毛利仅老板可见";
  render();
}

function render() {
  const emptyMessage = $("#search").value.trim() ? "没有找到匹配设备" : ({today_intake:"今天还没有入库",today_sold:"今天还没有出库",reserved:"当前没有预订设备",pending_pickup:"当前没有已售待取设备",in_stock:"当前没有在库设备",unprinted:"当前没有待打印标签",aged:"当前没有超30天库存"}[listScope] || "没有库存设备");
  $("#device-list").innerHTML = devices.length ? devices.map(device => `
    <article class="device" data-id="${device.id}">
      <div class="device-main">
        <span>
          <strong>${esc(device.model)} · ${esc(device.storage)}</strong>
          <p>${esc(device.color)} · 电池 ${device.battery_health ?? "-"}%</p>
          <small>${esc(device.stock_code)} · IMEI 尾号 ${esc(device.imei_tail || "-")}
            ${device.print_status === "printed" ? `<i class="print-state">已打印</i>` : ""}
          </small>
        </span>
        <span>
          <b>${money(device.list_price)}</b>
          <i class="pill">${statusNames[device.status] || device.status}</i>
          ${user.role === "owner" ? `<small>成本 ${money(device.purchase_cost)}</small>` : ""}
        </span>
      </div>
      <div class="device-actions">
        <button data-action="detail">详情</button>
        <button data-action="sell" ${["sold", "scrapped"].includes(device.status) ? "disabled" : ""}>出库</button>
        ${device.print_status === "printed" ? "" : `<button class="print-button" data-action="print">打印标签</button>`}
      </div>
    </article>`).join("") : `<div class="empty">${emptyMessage}</div>`;
}

async function printLabel(deviceId, button) {
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "正在打印…";
  }
  try {
    await api.printLabel(deviceId);
    toast("标签已发送到 NIIMBOT B1");
    await refresh();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function intakeWithCostConfirmation(data) {
  try {
    return await api.intake(data);
  } catch (error) {
    if (error.message.includes("销售标价低于收货成本") && confirm("销售标价低于收货成本，确认仍然入库吗？")) {
      return api.intake({ ...data, allowBelowCost: true });
    }
    throw error;
  }
}

function openSale(device) {
  $("#sale-form").reset();
  $("#sale-title").textContent = `出库：${device.model} ${device.storage}`;
  $("#sale-form").elements.deviceId.value = device.id;
  $("#sale-form").elements.salePrice.value = device.list_price;
  $("#sale-dialog").showModal();
}

async function openDetail(deviceId) {
  const detail = await api.device(deviceId); currentDetail = detail;
  const form = $("#detail-form");
  $("#detail-title").textContent = `${detail.model} · ${detail.stock_code}`;
  const values = {deviceId:detail.id,model:detail.model,storage:detail.storage,color:detail.color,systemVersion:detail.system_version,batteryHealth:detail.battery_health,chargeCycles:detail.charge_cycles,conditionGrade:detail.condition_grade,listPrice:detail.list_price,area:detail.area,notes:detail.notes};
  Object.entries(values).forEach(([key,value]) => { if(form.elements[key]) form.elements[key].value = value ?? ""; });
  $("#detail-status").value = detail.status;
  $("#detail-private").innerHTML = detail.imei ? `<section class="settings-block"><strong>IMEI：${esc(detail.imei)}</strong><small>IMEI2：${esc(detail.imei2 || "-")} · 序列号：${esc(detail.serial_number || "-")}</small>${user.role === "owner" ? `<small>收货成本：${money(detail.purchase_cost)}</small>` : ""}</section>` : "";
  $("#detail-photos").innerHTML = detail.photos.map(photo => `<img src="/api/photos/${encodeURIComponent(photo.id)}" alt="${esc(photo.description)}">`).join("");
  $("#detail-events").innerHTML = detail.events.map(item => `<div class="event-row"><strong>${esc(item.event_type)} · ${esc(item.actor_name)}</strong><small>${new Date(item.created_at).toLocaleString("zh-CN")} ${esc(item.note || "")}</small></div>`).join("");
  $("#workflow-status").textContent = detail.reservation ? `预订客户：${detail.reservation.customer_name}，订金 ${money(detail.reservation.deposit)}` : detail.repair ? `送修：${detail.repair.issue}（${detail.repair.vendor || "未填维修方"}）` : `当前状态：${statusNames[detail.status] || detail.status}`;
  $("#reserve-device").hidden = detail.status !== "in_stock";
  $("#cancel-reservation").hidden = !detail.reservation;
  $("#repair-device").hidden = ["sold","in_repair"].includes(detail.status);
  $("#complete-repair").hidden = !detail.repair;
  $("#return-device").hidden = detail.status !== "sold";
  $("#suggest-price").hidden = true;
  $("#detail-print").hidden = detail.print_status === "printed";
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
}

async function compressImage(file) {
  const source = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = source;
    await image.decode();
    const maxSide = 1800;
    const scale = Math.min(1, maxSide / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.9);
  } finally {
    URL.revokeObjectURL(source);
  }
}

async function scanImageData(file) {
  const rawSupported = /^image\/(jpeg|jpg|png|webp)$/i.test(file.type || "");
  if (rawSupported && file.size <= 9_500_000) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("照片读取失败，请重新拍摄"));
      reader.readAsDataURL(file);
    });
  }
  const source = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = source;
    await image.decode();
    const maxSide = 3600;
    const scale = Math.min(1, maxSide / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.96);
  } finally {
    URL.revokeObjectURL(source);
  }
}

function showOcrResult(result) {
  const values = [
    ["型号", result.model, "wide"],
    ["IMEI", result.imei, "wide"],
    ["颜色", result.color || "未识别"],
    ["系统", result.systemVersion || "未识别"],
    ["电池", result.batteryHealth == null ? "未识别" : `${result.batteryHealth}%`],
    ["充电", result.chargeCycles == null ? "未识别" : `${result.chargeCycles}次`]
  ];
  $("#ocr-result").innerHTML = values.map(([label, value, className]) =>
    `<div class="${className || ""}"><small>${label}</small><strong>${esc(value)}</strong></div>`
  ).join("");
  $("#ocr-result").hidden = false;
  $("#screenshot-fields").hidden = false;
  if (result.storage) $("#screenshot-form").elements.storage.value = result.storage;
  $("#screenshot-submit").disabled = false;
}

function resetScreenshotForm() {
  recognizedScreenshot = null;
  $("#screenshot-form").reset();
  $("#ocr-result").hidden = true;
  $("#screenshot-fields").hidden = true;
  $("#screenshot-submit").disabled = true;
  $("#screenshot-error").textContent = "";
  $("#ocr-status").className = "ocr-status";
  $("#ocr-status").textContent = "选择截图后会自动识别型号、颜色、系统、电池、充电次数和IMEI。";
}

$("#setup-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#setup-error").textContent = "";
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api.setup(data);
    await api.login(data.username, data.password);
    user = await api.me();
    await loadApp();
  } catch (error) {
    $("#setup-error").textContent = error.message;
  }
});

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#login-error").textContent = "";
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api.login(data.username, data.password);
    user = await api.me();
    await loadApp();
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api.logout();
  user = null;
  show("login");
});

$("#refresh").addEventListener("click", () => refresh().then(() => toast("已刷新")).catch(error => toast(error.message)));
$("#settings-button").addEventListener("click", async () => {
  $("#staff-form").hidden = user.role !== "owner";
  $("#import-block").hidden = user.role !== "owner";
  $("#settings-dialog").showModal();
  try { await Promise.all([refreshSystemStatus(), renderUsers(), renderBackups()]); } catch (error) { toast(error.message); }
});
$("#report-button").addEventListener("click",()=>{
  const now=new Date(),first=new Date(now.getFullYear(),now.getMonth(),1); const local=date=>new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,10);
  $("#report-form").elements.from.value=local(first); $("#report-form").elements.to.value=local(now); $("#report-result").innerHTML=""; $("#report-dialog").showModal(); $("#report-form").requestSubmit();
});
const localDay = date => new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,10);
async function loadLedger() {
  const day = $("#ledger-form").elements.date.value;
  const result = await api.ledger(day);
  ledgerRows = result.rows;
  const s = result.summary;
  $("#ledger-summary").innerHTML = `<article><small>售出</small><strong>${s.count} 台</strong></article><article><small>成交额</small><strong>${money(s.revenue)}</strong></article>${user.role==="owner"?`<article><small>利润</small><strong>${money(s.profit)}</strong></article>`:""}<article class="gift-total"><small>赠品合计</small><strong>壳 ${s.giftCase} · 膜 ${s.giftScreenProtector} · 充电头 ${s.giftChargingHead} · 充电器 ${s.giftCharger}</strong></article>`;
  $("#ledger-head").innerHTML = `<tr><th>时间</th><th>型号 / 容量</th><th>串号</th><th>赠品</th><th>成交价</th>${user.role==="owner"?"<th>回收价</th><th>利润</th><th>更正</th>":""}</tr>`;
  $("#ledger-body").innerHTML = result.rows.length ? result.rows.map(row => {
    const gifts = [[row.gift_case,"壳"],[row.gift_screen_protector,"膜"],[row.gift_charging_head,"充电头"],[row.gift_charger,"充电器"]].filter(x=>x[0]).map(x=>x[1]).join("、") || "无";
    const time = new Date(row.sold_at).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"});
    return `<tr class="${row.returned?"returned":""}"><td>${time}${row.returned?"<em>已退货</em>":""}</td><td><b>${esc(row.model)}</b><small>${esc(row.storage)} · ${esc(row.sold_by_name)}</small></td><td>${esc(row.imei)}</td><td>${gifts}</td><td>${money(row.sale_price)}</td>${user.role==="owner"?`<td>${money(row.purchase_cost_snapshot)}</td><td>${money(row.sale_price-row.purchase_cost_snapshot)}</td><td><button type="button" data-edit-sale="${row.id}">修改</button></td>`:""}</tr>`;
  }).join("") : `<tr><td colspan="8" class="empty-cell">当天暂无销售记录</td></tr>`;
  $("#ledger-export").href = `/api/export/ledger.csv?date=${encodeURIComponent(day)}`;
}
$("#ledger-button").addEventListener("click", async()=>{
  $("#ledger-form").elements.date.value=localDay(new Date());
  $("#ledger-dialog").showModal();
  try{await loadLedger();}catch(error){toast(error.message);}
});
$("#ledger-form").addEventListener("submit",async event=>{event.preventDefault();try{await loadLedger();}catch(error){toast(error.message);}});
$("#ledger-body").addEventListener("click",event=>{
  const button=event.target.closest("[data-edit-sale]"); if(!button)return;
  const row=ledgerRows.find(item=>item.id===button.dataset.editSale); if(!row)return;
  const form=$("#ledger-edit-form"); form.reset(); form.elements.saleId.value=row.id; form.elements.salePrice.value=row.sale_price; form.elements.paymentMethod.value=row.payment_method || "微信"; form.elements.customerNote.value=row.customer_note || "";
  form.elements.giftCase.checked=!!row.gift_case; form.elements.giftScreenProtector.checked=!!row.gift_screen_protector; form.elements.giftChargingHead.checked=!!row.gift_charging_head; form.elements.giftCharger.checked=!!row.gift_charger;
  $("#ledger-edit-dialog").showModal();
});
$("#ledger-edit-form").addEventListener("submit",async event=>{event.preventDefault();$("#ledger-edit-error").textContent="";const data=Object.fromEntries(new FormData(event.currentTarget));try{await api.updateSale(data.saleId,data);$("#ledger-edit-dialog").close();await loadLedger();await refresh();toast("销售记录已更正");}catch(error){$("#ledger-edit-error").textContent=error.message;}});
$("#ledger-print").addEventListener("click",()=>window.print());

const marketRepairNames={original:"全原装",no_repair:"无拆修",minor_repair:"小修",major_repair:"大修",unknown:"未知"};
const marketConfidenceNames={high:"较高",medium:"中等",low:"较低"};
function marketFilters(){return Object.fromEntries(new FormData($("#market-query-form")));}
function priceRange(value){return value?`${money(value[0])} ～ ${money(value[1])}`:"证据不足";}
function renderMarketSummary(result){
  const e=result.external,i=result.internal,s=result.suggestion;
  $("#market-summary").innerHTML=`<div class="market-price-grid"><article class="market-price"><small>建议回收区间</small><strong>${priceRange(s.purchaseRange)}</strong><em>外部回收中位数 ${e.recycleMedian?money(e.recycleMedian):"—"}</em></article><article class="market-price"><small>建议销售区间</small><strong>${priceRange(s.saleRange)}</strong><em>店内成交中位数 ${i.salesMedian?money(i.salesMedian):"—"}</em></article><article class="market-price"><small>预计最低毛利</small><strong>${s.estimatedMargin===null?"—":money(s.estimatedMargin)}</strong><em>证据可信度：${marketConfidenceNames[s.confidence]}</em></article></div><section class="market-evidence"><div><b>外部参考</b><span>回收 ${e.recycleCount} 条 · 销售 ${e.retailCount} 条</span><small>${e.sources.length?e.sources.map(esc).join("、"):"尚未录入外部来源"}</small></div><div><b>门店自己的数据</b><span>成交 ${i.salesCount} 笔 · 同款库存 ${i.inventoryCount} 台</span><small>历史成本中位数 ${i.costMedian?money(i.costMedian):"—"} · 当前标价中位数 ${i.listMedian?money(i.listMedian):"—"}</small></div></section><p class="market-basis">依据：${esc(s.basis)}。${esc(result.notice)}</p>`;
}
function renderMarketQuotes(rows){
  $("#market-quotes").innerHTML=rows.length?rows.map(row=>`<div class="market-row"><span><b>${esc(row.source_name)} · ${row.quote_type==="recycle"?"回收":"销售"}</b><small>${esc(row.model)} ${esc(row.storage)} · ${esc(row.condition_grade||"成色未标")} · 电池${row.battery_health??"未标"} · ${marketRepairNames[row.repair_status]||"未知"}</small><small>${esc(row.captured_on)} ${row.note?`· ${esc(row.note)}`:""}</small></span><strong>${money(row.price)}</strong><button type="button" data-delete-quote="${row.id}">删除</button></div>`).join(""):"<p class='empty-inline'>尚无匹配行情，先录入一条实际报价。</p>";
}
function renderPricingDecisions(rows){
  $("#pricing-decisions").innerHTML=rows.length?rows.map(row=>`<div class="market-row"><span><b>${esc(row.model)} ${esc(row.storage)}</b><small>${new Date(row.created_at).toLocaleString("zh-CN")} · ${esc(row.creator_name)}</small><small>${esc(row.adjustment_reason||"未填写调整原因")}</small></span><strong>收 ${row.final_purchase_price===null?"—":money(row.final_purchase_price)}<br>售 ${row.final_sale_price===null?"—":money(row.final_sale_price)}</strong></div>`).join(""):"<p class='empty-inline'>还没有保存过老板定价。</p>";
}
function renderMarketSheet(result){
  marketSheetResult=result;
  $("#market-sheet-date").value=result.capturedOn||localDay(new Date());
  $("#market-sheet-count").value=`${result.rowCount}/${result.expectedRowCount??result.rowCount} 个容量 · ${result.quoteCount} 条报价`;
  const conditions=["高保充新","靓机","小花","大花","外爆","内爆可测"];
  $("#market-sheet-body").innerHTML=result.rows.map((row,index)=>`<tr data-sheet-index="${index}"><td><input type="checkbox" data-field="include" checked aria-label="导入第${index+1}行"></td><td><input data-field="model" value="${esc(row.model)}"></td><td><input data-field="storage" value="${esc(row.storage)}"></td>${conditions.map(condition=>`<td><input data-condition="${condition}" type="number" min="0" value="${row.prices[condition]??""}"></td>`).join("")}</tr>`).join("");
  $("#market-sheet-preview").hidden=false;
  $("#market-sheet-import").disabled=result.complete===false;
  $("#market-sheet-status").className=`ocr-status ${result.complete===false?"":"done"}`;
  $("#market-sheet-status").textContent=result.complete===false?`完整性校验未通过：表格应有${result.expectedRowCount}行，识别到${result.rowCount}行。已禁止导入，请勿使用缺失行情。`:`完整性校验通过：${result.rowCount}个容量、${result.quoteCount}条分档报价。请抽查后确认导入。`;
}
const feedStatusNames={success:"校验通过并已导入",rejected:"校验失败，未导入",error:"同步失败",unchanged:"今日已导入"};
async function loadMarketFeedStatus(){
  const status=await api.marketFeedStatus();
  $("#market-feed-summary").textContent=`${status.schedule}。${status.scope}${status.busy?"；当前正在同步。":""}`;
  $("#market-feed-pages").innerHTML=status.pages.map(page=>{const run=page.lastRun;const state=run?.status||"";const detail=run?`${feedStatusNames[state]||state} · ${esc(run.captured_on)} · ${run.row_count}/${run.expected_row_count}行 · 新增${run.imported_count}条${run.message?` · ${esc(run.message)}`:""}`:"尚未同步";return `<div class="market-feed-row ${state}"><span><b>${esc(page.sourceName)}</b><small>${detail}</small><small>${esc(page.pageUrl)}</small></span><button type="button" data-feed-sync="${page.pageId}">立即同步</button></div>`;}).join("");
}
async function loadMarket(){
  const filters=marketFilters();
  $("#market-error").textContent="";
  const [summary,quotes,decisions]=await Promise.all([api.marketSummary(filters),api.marketQuotes(filters),api.pricingDecisions(filters)]);
  marketEvidence=summary; renderMarketSummary(summary); renderMarketQuotes(quotes); renderPricingDecisions(decisions);
}
function openMarketDialog(device=null){
  marketEvidence=null; $("#market-summary").innerHTML='<div class="empty">输入型号和容量后查询行情</div>'; $("#market-error").textContent="";
  const form=$("#market-query-form");
  if(device){form.elements.brand.value=device.brand||"其他";form.elements.model.value=device.model||"";form.elements.storage.value=device.storage||"";form.elements.conditionGrade.value=device.condition_grade||"";form.elements.batteryHealth.value=device.battery_health??"";form.elements.repairStatus.value="unknown";}
  $("#market-quote-form").elements.capturedOn.value=localDay(new Date()); $("#market-dialog").showModal();
  loadMarketFeedStatus().catch(error=>{$("#market-feed-summary").textContent=error.message;});
  if(device) form.requestSubmit();
}
$("#market-feed-pages").addEventListener("click",async event=>{const button=event.target.closest("[data-feed-sync]");if(!button)return;button.disabled=true;button.textContent="同步中…";$("#market-error").textContent="";try{const result=await api.syncMarketFeed(button.dataset.feedSync);toast(`${result.sourceName}：${result.message}`);await loadMarketFeedStatus();}catch(error){$("#market-error").textContent=error.message;await loadMarketFeedStatus().catch(()=>{});}finally{button.disabled=false;button.textContent="立即同步";}});
$("#market-sheet-form").addEventListener("submit",async event=>{
  event.preventDefault(); const form=event.currentTarget; const button=event.submitter; button.disabled=true; $("#market-error").textContent=""; $("#market-sheet-status").className="ocr-status working"; $("#market-sheet-status").textContent="正在下载、按表格线识别并校验长报价表，通常需要1至3分钟…";
  try{const result=await api.recognizeMarketSheet(Object.fromEntries(new FormData(form)));renderMarketSheet(result);}catch(error){$("#market-sheet-status").className="ocr-status";$("#market-sheet-status").textContent=error.message;}finally{button.disabled=false;}
});
$("#market-sheet-body").addEventListener("change",event=>{const checkbox=event.target.closest('[data-field="include"]');if(checkbox)checkbox.closest("tr").classList.toggle("skipped",!checkbox.checked);});
$("#market-sheet-cancel").addEventListener("click",()=>{marketSheetResult=null;$("#market-sheet-preview").hidden=true;$("#market-sheet-status").className="ocr-status";$("#market-sheet-status").textContent="本次识别已取消，没有写入行情库。";});
$("#market-sheet-import").addEventListener("click",async event=>{
  if(!marketSheetResult)return; const conditions=["高保充新","靓机","小花","大花","外爆","内爆可测"]; const selected=[];
  document.querySelectorAll("#market-sheet-body tr").forEach(tr=>{if(!tr.querySelector('[data-field="include"]').checked)return;const original=marketSheetResult.rows[Number(tr.dataset.sheetIndex)];const prices={};conditions.forEach(condition=>{const value=tr.querySelector(`[data-condition="${condition}"]`).value;if(value!=="")prices[condition]=Number(value);});selected.push({...original,model:tr.querySelector('[data-field="model"]').value.trim(),storage:tr.querySelector('[data-field="storage"]').value.trim(),prices});});
  if(!selected.length){$("#market-error").textContent="请至少勾选一行报价";return;} if(!confirm(`确认把选中的${selected.length}个容量、最多${selected.length*6}条行情写入本地数据库？`))return;
  const button=event.currentTarget;button.disabled=true;$("#market-error").textContent="";
  try{const result=await api.importMarketSheet({sourceName:marketSheetResult.sourceName,imageUrl:marketSheetResult.imageUrl,sheetRef:marketSheetResult.sheetRef,capturedOn:$("#market-sheet-date").value,rows:selected});$("#market-sheet-preview").hidden=true;marketSheetResult=null;$("#market-sheet-status").className="ocr-status done";$("#market-sheet-status").textContent=`批量导入完成：新增${result.imported}条，跳过重复${result.skipped}条。`;toast("整张报价表已导入行情库");}catch(error){$("#market-error").textContent=error.message;}finally{button.disabled=false;}
});
$("#market-query-form").addEventListener("submit",async event=>{event.preventDefault();$("#market-summary").innerHTML='<div class="empty">正在汇总外部行情和门店历史…</div>';try{await loadMarket();}catch(error){$("#market-error").textContent=error.message;}});
$("#market-quote-form").addEventListener("submit",async event=>{
  event.preventDefault(); const form=event.currentTarget; const query=$("#market-query-form"); if(!query.reportValidity())return;
  const button=event.submitter; button.disabled=true; $("#market-error").textContent="";
  try{await api.createMarketQuote({...marketFilters(),...Object.fromEntries(new FormData(form))});form.elements.price.value="";form.elements.note.value="";await loadMarket();toast("外部行情已保存");}catch(error){$("#market-error").textContent=error.message;}finally{button.disabled=false;}
});
$("#pricing-decision-form").addEventListener("submit",async event=>{
  event.preventDefault(); const form=event.currentTarget; if(!marketEvidence){$("#market-error").textContent="请先查询一次行情，再保存最终定价";return;}
  const button=event.submitter; button.disabled=true; $("#market-error").textContent="";
  try{const finalData=Object.fromEntries(new FormData(form));await api.createPricingDecision({...marketFilters(),...finalData,suggestion:marketEvidence.suggestion,evidence:{external:marketEvidence.external,internal:marketEvidence.internal}});form.reset();await loadMarket();toast("老板定价已保存");}catch(error){$("#market-error").textContent=error.message;}finally{button.disabled=false;}
});
$("#market-quotes").addEventListener("click",async event=>{const button=event.target.closest("[data-delete-quote]");if(!button||!confirm("确认删除这条错误行情记录？"))return;button.disabled=true;try{await api.deleteMarketQuote(button.dataset.deleteQuote);await loadMarket();toast("行情记录已删除");}catch(error){$("#market-error").textContent=error.message;button.disabled=false;}});

$("#report-form").addEventListener("submit",async event=>{
  event.preventDefault(); const d=Object.fromEntries(new FormData(event.currentTarget));
  try{const r=await api.report(d.from,d.to);$("#report-result").innerHTML=`<div class="report-metrics"><article><small>售出</small><strong>${r.soldCount}台</strong></article><article><small>销售额</small><strong>${money(r.revenue)}</strong></article><article><small>退款</small><strong>${money(r.refundAmount)}</strong></article><article><small>净销售额</small><strong>${money(r.netRevenue)}</strong></article>${user.role==="owner"?`<article><small>净利润</small><strong>${money(r.netProfit)}</strong></article><article><small>库存成本</small><strong>${money(r.inventoryCost)}</strong></article>`:""}</div><section class="settings-block"><strong>库存老化</strong>${r.aging.map(x=>`<div class="user-row"><span>${esc(x.bucket)}</span><b>${x.count}台</b></div>`).join("")}</section><section class="settings-block"><strong>型号销量</strong>${r.models.map(x=>`<div class="user-row"><span>${esc(x.model)} ${esc(x.storage)}</span><b>${x.count}台 · ${money(x.revenue)}</b></div>`).join("")||"暂无销售"}</section><section class="settings-block"><strong>店员业绩</strong>${r.staff.map(x=>`<div class="user-row"><span>${esc(x.display_name)}</span><b>${x.sold_count}台 · ${money(x.revenue)}</b></div>`).join("")||"暂无销售"}</section>`;}catch(error){$("#report-result").textContent=error.message;}
});
$("#backup-now").addEventListener("click", async () => {
  $("#backup-status").textContent = "正在备份…";
  try {
    const result = await api.createBackup();
    $("#backup-status").textContent = `备份完成并校验通过：${result.name}`;
    await refreshSystemStatus();
    await renderBackups();
  } catch (error) { $("#backup-status").textContent = error.message; }
});
$("#password-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#password-error").textContent = "";
  try {
    await api.changePassword(Object.fromEntries(new FormData(event.currentTarget)));
    alert("密码修改成功，请使用新密码重新登录。");
    location.reload();
  } catch (error) { $("#password-error").textContent = error.message; }
});
$("#staff-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#staff-error").textContent = "";
  try {
    await api.createUser(Object.fromEntries(new FormData(event.currentTarget)));
    event.currentTarget.reset(); event.currentTarget.elements.role.value = "staff";
    toast("店员账号已添加"); await renderUsers();
  } catch (error) { $("#staff-error").textContent = error.message; }
});
$("#import-submit").addEventListener("click",async()=>{const file=$("#import-file").files[0];if(!file){toast("请先选择CSV文件");return;}if(!confirm("确认把CSV中的设备加入正式库存？系统会跳过重复IMEI。"))return;$("#import-status").textContent="正在导入…";try{const result=await api.importCsv(await file.text());$("#import-status").textContent=`成功${result.imported}条，失败${result.errors.length}条${result.errors.length?`：${result.errors.map(x=>`第${x.row}行${x.error}`).join("；")}`:""}`;await refresh();}catch(error){$("#import-status").textContent=error.message;}});
$("#new-intake").addEventListener("click", () => {
  resetScreenshotForm();
  $("#screenshot-dialog").showModal();
});
function stopScanCamera() {
  scanLoopToken += 1;
  if (scanStream) scanStream.getTracks().forEach(track => track.stop());
  scanStream = null;
  const video = $("#scan-video");
  video.pause();
  video.srcObject = null;
  video.hidden = true;
}

function resetScanFlow() {
  stopScanCamera();
  scanCandidate = null;
  scanBusy = false;
  scanLastFrame = 0;
  $("#scan-form").reset();
  $("#scan-sale-form").reset();
  $("#scan-file").value = "";
  $("#scan-code").value = "";
  $("#scan-error").textContent = "";
  $("#scan-match").hidden = true;
  $("#scan-camera-panel").hidden = false;
  $("#scan-form").hidden = false;
  $("#scan-camera-status").textContent = "请把库存标签二维码放入框内";
}

function liveCameraAvailable() {
  return window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;
}

async function startScanCamera() {
  stopScanCamera();
  if (!liveCameraAvailable()) {
    $("#scan-camera-status").textContent = "局域网模式将使用手机原生相机拍照扫码";
    return false;
  }
  $("#scan-camera-status").textContent = "正在打开后置摄像头…";
  try {
    scanStream = await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:"environment"},width:{ideal:1280},height:{ideal:720}},audio:false});
    const video = $("#scan-video");
    video.srcObject = scanStream;
    video.hidden = false;
    await video.play();
    $("#scan-camera-status").textContent = "请将标签二维码对准扫描框";
    const token = ++scanLoopToken;
    scanVideoLoop(token);
    return true;
  } catch (error) {
    stopScanCamera();
    $("#scan-camera-status").textContent = "摄像头未能打开，请点“拍照扫码”";
    return false;
  }
}

async function scanVideoLoop(token) {
  if (token !== scanLoopToken || !scanStream || scanCandidate) return;
  const video = $("#scan-video");
  const now = performance.now();
  if (!scanBusy && video.readyState >= 2 && now - scanLastFrame > 700) {
    scanLastFrame = now;
    try {
      let value = "";
      if ("BarcodeDetector" in window) {
        scanDetector ||= new BarcodeDetector({formats:["qr_code"]});
        const codes = await scanDetector.detect(video);
        value = codes[0]?.rawValue || "";
      } else {
        const canvas = $("#scan-canvas");
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext("2d").drawImage(video,0,0,canvas.width,canvas.height);
        const result = await api.recognizeQr(canvas.toDataURL("image/jpeg",.72));
        value = result.value;
      }
      if (value) await resolveScanCode(value);
    } catch { /* 下一帧继续扫描，不把未对准当成错误 */ }
  }
  if (token === scanLoopToken && scanStream && !scanCandidate) setTimeout(() => scanVideoLoop(token), 160);
}

function showScanCandidate(device) {
  scanCandidate = device;
  stopScanCamera();
  const imeiTail = device.imei ? String(device.imei).slice(-4) : device.imei_tail || "-";
  const canSell = ["in_stock","reserved","sold_pending_pickup"].includes(device.status);
  $("#scan-device-card").innerHTML = `<article class="scan-device"><div><small>库存编号</small><strong>${esc(device.stock_code)}</strong></div><div><small>当前状态</small><strong>${esc(statusNames[device.status] || device.status)}</strong></div><div class="wide"><small>设备</small><strong>${esc(device.model)} · ${esc(device.storage)}</strong></div><div><small>颜色</small><strong>${esc(device.color || "-")}</strong></div><div><small>电池</small><strong>${device.battery_health ?? "-"}%</strong></div><div><small>IMEI尾号</small><strong>${esc(imeiTail)}</strong></div><div><small>库位</small><strong>${esc(device.area || "-")}</strong></div><div class="wide price"><small>当前标价</small><strong>${money(device.list_price)}</strong></div></article>`;
  const saleForm = $("#scan-sale-form");
  saleForm.elements.deviceId.value = device.id;
  saleForm.elements.salePrice.value = device.list_price;
  $("#scan-confirm-sale").disabled = !canSell;
  $("#scan-error").textContent = canSell ? "核对设备无误后，再点击确认出库。" : `该设备当前为“${statusNames[device.status] || device.status}”，不能出库。`;
  $("#scan-camera-panel").hidden = true;
  $("#scan-form").hidden = true;
  $("#scan-match").hidden = false;
}

async function resolveScanCode(rawCode) {
  const code = String(rawCode || "").trim();
  if (!code || scanBusy) return;
  scanBusy = true;
  $("#scan-error").textContent = "已扫码，正在匹配库存…";
  try {
    const matches = await api.devices(code, "");
    const normalized = code.toLowerCase();
    const exact = matches.filter(item => String(item.stock_code || "").toLowerCase() === normalized || String(item.imei || "") === code);
    const selected = exact.length === 1 ? exact[0] : (!exact.length && matches.length === 1 ? matches[0] : null);
    if (!selected && matches.length > 1) throw new Error("找到多台相似设备，请输入完整库存编号或完整IMEI");
    if (!selected) throw new Error("没有找到对应库存，请检查标签或编号");
    const detail = await api.device(selected.id);
    showScanCandidate({...selected,...detail,imei_tail:selected.imei_tail});
    if (navigator.vibrate) navigator.vibrate(80);
  } catch (error) {
    $("#scan-error").textContent = error.message;
    if (navigator.vibrate) navigator.vibrate([100,60,100]);
  } finally {
    scanBusy = false;
  }
}

function openScanner() {
  if (liveCameraAvailable()) startScanCamera();
  else {
    $("#scan-camera-status").textContent = "正在打开手机相机，请拍摄标签二维码";
    $("#scan-file").click();
  }
}

$("#scan-outbound").addEventListener("click", () => {
  resetScanFlow();
  $("#scan-dialog").showModal();
  openScanner();
});
$("#scan-start-camera").addEventListener("click", openScanner);
$("#scan-photo").addEventListener("click", () => $("#scan-file").click());
$("#scan-file").addEventListener("change", async event => {
  const file = event.target.files[0]; if (!file) return;
  $("#scan-error").textContent = "正在识别标签二维码…";
  try { const result = await api.recognizeQr(await scanImageData(file)); await resolveScanCode(result.value); }
  catch (error) { $("#scan-error").textContent = error.message; }
  finally { event.target.value = ""; }
});
$("#scan-form").addEventListener("submit", async event => {
  event.preventDefault();
  await resolveScanCode($("#scan-code").value);
});
$("#scan-again").addEventListener("click", () => { resetScanFlow(); openScanner(); });
$("#scan-sale-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!scanCandidate) return;
  const button = $("#scan-confirm-sale");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "正在出库…";
  try {
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await api.sell(scanCandidate.id, data);
    $("#scan-dialog").close();
    toast(`出库成功：${scanCandidate.stock_code}`);
    await refresh();
  } catch (error) {
    $("#scan-error").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});
$("#scan-dialog").addEventListener("close", stopScanCamera);

$("#screenshot-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (!file) return;
  recognizedScreenshot = null;
  $("#screenshot-submit").disabled = true;
  $("#screenshot-fields").hidden = true;
  $("#ocr-result").hidden = true;
  $("#screenshot-error").textContent = "";
  $("#ocr-status").className = "ocr-status working";
  $("#ocr-status").textContent = "正在电脑本地识别，首次使用大约需要6秒…";
  try {
    const image = await compressImage(file);
    recognizedScreenshot = await api.recognizeScreenshot(image);
    showOcrResult(recognizedScreenshot);
    $("#ocr-status").className = "ocr-status done";
    $("#ocr-status").textContent = "识别完成。核对IMEI，再补充容量和价格即可入库。";
    $("#screenshot-form").elements[recognizedScreenshot.storage ? "purchaseCost" : "storage"].focus();
  } catch (error) {
    $("#ocr-status").className = "ocr-status";
    $("#ocr-status").textContent = "没有完成识别";
    $("#screenshot-error").textContent = error.message;
  }
});

$("#screenshot-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!recognizedScreenshot) return;
  $("#screenshot-error").textContent = "";
  const extra = Object.fromEntries(new FormData(event.currentTarget));
  const printAfterIntake = extra.printAfterIntake === "on";
  delete extra.printAfterIntake;
  const data = { ...recognizedScreenshot, ...extra };
  delete data.confidence;
  delete data.recognizedBy;
  try {
    const result = await intakeWithCostConfirmation(data);
    $("#screenshot-dialog").close();
    resetScreenshotForm();
    if (printAfterIntake) {
      toast(`已入库 ${result.stockCode}，正在打印…`);
      try {
        await printLabel(result.id);
      } catch (printError) {
        toast(`已入库，但打印失败：${printError.message}`);
      }
    } else {
      toast(`截图入库成功：${result.stockCode}`);
    }
    await refresh();
  } catch (error) {
    $("#screenshot-error").textContent = error.message;
  }
});

$("#manual-intake").addEventListener("click", () => {
  $("#screenshot-dialog").close();
  $("#intake-dialog").showModal();
});

$("#access-button").addEventListener("click", async () => {
  const result = await api.access();
  $("#access-url").textContent = result.url;
  $("#access-qr").src = `/api/qrcode.svg?t=${Date.now()}`;
  $("#access-dialog").showModal();
});

document.addEventListener("click", event => {
  const userToggle=event.target.closest("[data-user-toggle]");
  if(userToggle){api.toggleUser(userToggle.dataset.userToggle).then(()=>renderUsers()).catch(error=>toast(error.message));return;}
  const restore = event.target.closest("[data-restore]");
  if (restore) {
    const name = restore.dataset.restore;
    if (confirm(`确认恢复备份 ${name}？当前数据库会先自动做一份安全备份。`)) {
      restore.disabled = true;
      api.restoreBackup(name).then(result => { alert(`恢复完成：${result.restored}`); location.reload(); }).catch(error => { restore.disabled = false; toast(error.message); });
    }
    return;
  }
  const close = event.target.closest("[data-close]");
  if (close) $(`#${close.dataset.close}`).close();
  const filter = event.target.closest("[data-scope]");
  if (filter) {
    listScope = filter.dataset.scope;
    $("#list-title").textContent = scopeNames[listScope] || "手机库存";
    document.querySelectorAll("[data-scope]").forEach(item => item.classList.toggle("active", item.dataset.scope === listScope));
    refresh().catch(error => toast(error.message));
  }
});

$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => refresh().catch(error => toast(error.message)), 250);
});

$("#device-list").addEventListener("click", event => {
  const action = event.target.closest("[data-action]");
  const card = event.target.closest("[data-id]");
  if (!action || !card) return;
  const device = devices.find(item => item.id === card.dataset.id);
  if (action.dataset.action === "print") {
    printLabel(device.id, action).catch(error => toast(error.message));
    return;
  }
  if (action.dataset.action === "detail") { openDetail(device.id).catch(error=>toast(error.message)); return; }
  openSale(device);
});

$("#detail-print").addEventListener("click", event => { if(currentDetail) printLabel(currentDetail.id,event.currentTarget).catch(error=>toast(error.message)); });
$("#reserve-device").addEventListener("click", async () => {
  const customerName=prompt("客户姓名或称呼："); if(!customerName)return;
  const customerPhone=prompt("客户电话（可留空）：")||""; const deposit=prompt("订金金额：","0")||"0"; const expiresAt=prompt("预留截止时间（可留空，例如2026-07-18 18:00）：")||"";
  try{await api.reserve(currentDetail.id,{customerName,customerPhone,deposit,expiresAt});toast("预订成功");await refresh();await openDetail(currentDetail.id);}catch(error){toast(error.message);}
});
$("#cancel-reservation").addEventListener("click",async()=>{if(!confirm("确认取消该预订并恢复在库？"))return;try{await api.cancelReservation(currentDetail.id);toast("预订已取消");await refresh();await openDetail(currentDetail.id);}catch(error){toast(error.message);}});
$("#repair-device").addEventListener("click",async()=>{const issue=prompt("送修问题：");if(!issue)return;const vendor=prompt("维修方（可留空）：")||"";try{await api.startRepair(currentDetail.id,{issue,vendor});toast("已登记送修");await refresh();await openDetail(currentDetail.id);}catch(error){toast(error.message);}});
$("#complete-repair").addEventListener("click",async()=>{const cost=prompt("最终维修成本：","0")||"0";const scrap=confirm("点“确定”表示报废；点“取消”表示维修完成并恢复在库。");try{await api.completeRepair(currentDetail.id,{cost,status:scrap?"scrapped":"in_stock"});toast("维修流程已完成");await refresh();await openDetail(currentDetail.id);}catch(error){toast(error.message);}});
$("#return-device").addEventListener("click",async()=>{const reason=prompt("退货原因：");if(!reason)return;const refundAmount=prompt("退款金额：",String(currentDetail.latestSale?.sale_price||0));if(refundAmount===null)return;const disposition=prompt("后续处理：输入 restock重新入库 / repair送修 / scrap报废","restock")||"restock";try{await api.returnDevice(currentDetail.id,{reason,refundAmount,disposition});toast("退货已登记");await refresh();await openDetail(currentDetail.id);}catch(error){toast(error.message);}});
$("#make-sales-copy").addEventListener("click",async()=>{try{const r=await api.salesCopy(currentDetail.id);try{await navigator.clipboard.writeText(r.text);toast("销售文案已复制");}catch{prompt("复制销售文案：",r.text);}}catch(error){toast(error.message);}});
$("#detail-photo-add").addEventListener("click",async()=>{const file=$("#detail-photo-file").files[0];if(!file){toast("请先选择照片");return;}try{await api.addPhoto(currentDetail.id,{image:await compressImage(file),description:$("#detail-photo-description").value});$("#detail-photo-file").value="";$("#detail-photo-description").value="";toast("照片已保存");await openDetail(currentDetail.id);}catch(error){toast(error.message);}});
$("#detail-form").addEventListener("submit", async event => {
  event.preventDefault(); if(!currentDetail)return; $("#detail-error").textContent="";
  const data=Object.fromEntries(new FormData(event.currentTarget)); delete data.deviceId;
  try {
    await api.updateDevice(currentDetail.id,data);
    if($("#detail-status").value!==currentDetail.status) await api.changeStatus(currentDetail.id,{status:$("#detail-status").value,note:"详情页修改"});
    toast("设备资料已保存"); await refresh(); await openDetail(currentDetail.id);
  } catch(error){ $("#detail-error").textContent=error.message; }
});

$("#intake-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#intake-error").textContent = "";
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const printAfterIntake = data.printAfterIntake === "on";
  delete data.printAfterIntake;
  try {
    const result = await intakeWithCostConfirmation(data);
    $("#intake-dialog").close();
    event.currentTarget.reset();
    if (printAfterIntake) {
      toast(`已入库 ${result.stockCode}，正在打印…`);
      try {
        await printLabel(result.id);
      } catch (printError) {
        toast(`已入库，但打印失败：${printError.message}`);
      }
    } else {
      toast(`入库成功：${result.stockCode}`);
    }
    await refresh();
  } catch (error) {
    $("#intake-error").textContent = error.message;
  }
});

$("#sale-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#sale-error").textContent = "";
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api.sell(data.deviceId, data);
    $("#sale-dialog").close();
    toast("出库成功");
    await refresh();
  } catch (error) {
    $("#sale-error").textContent = error.message;
  }
});

boot().catch(error => {
  show("login");
  $("#login-error").textContent = error.message;
});

setInterval(() => {
  if (user && document.visibilityState === "visible") refresh().catch(() => {});
}, 4000);
setInterval(() => {
  if (user && document.visibilityState === "visible") refreshSystemStatus().catch(() => {});
}, 60000);
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "127.0.0.1" || location.hostname === "localhost")) navigator.serviceWorker.register("/sw.js").catch(()=>{});
