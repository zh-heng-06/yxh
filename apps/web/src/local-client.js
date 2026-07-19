export class LocalClient {
  async request(path, { method="GET", body, timeout=15000 }={}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    let response;
    try {
      response = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: { ...(body?{"Content-Type":"application/json"}:{}), ...(method!=="GET"?{"X-ZhangGui-Request":"1"}:{}) },
        body: body ? JSON.stringify(body) : undefined,
        cache: "no-store",
        signal: controller.signal
      });
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("连接店内电脑超时，请确认手机已连接与电脑相同的Wi-Fi后重试");
      throw new Error("无法连接店内电脑，请确认手机已连接与电脑相同的Wi-Fi");
    } finally {
      clearTimeout(timer);
    }
    const result = await response.json().catch(()=>({error:`请求失败 (${response.status})`}));
    if (!response.ok) throw new Error(result.error || "请求失败");
    return result;
  }
  setupStatus(){return this.request("/api/setup-status");}
  setup(data){return this.request("/api/setup",{method:"POST",body:data});}
  login(username,password){return this.request("/api/login",{method:"POST",body:{username,password}});}
  logout(){return this.request("/api/logout",{method:"POST",body:{}});}
  me(){return this.request("/api/me");}
  status(){return this.request("/api/status");}
  backups(){return this.request("/api/backups");}
  createBackup(){return this.request("/api/backups/create",{method:"POST",body:{}});}
  restoreBackup(name){return this.request("/api/backups/restore",{method:"POST",body:{name,confirmation:"RESTORE"}});}
  users(){return this.request("/api/users");}
  createUser(data){return this.request("/api/users",{method:"POST",body:data});}
  toggleUser(id){return this.request(`/api/users/${encodeURIComponent(id)}/toggle`,{method:"POST",body:{}});}
  changePassword(data){return this.request("/api/password",{method:"POST",body:data});}
  dashboard(){return this.request("/api/dashboard");}
  report(from,to){const p=new URLSearchParams({from,to});return this.request(`/api/reports/summary?${p}`);}
  ledger(date){const p=new URLSearchParams({date});return this.request(`/api/ledger?${p}`);}
  updateSale(id,data){return this.request(`/api/sales/${encodeURIComponent(id)}/update`,{method:"POST",body:data});}
  marketQuotes(filters={}){const p=new URLSearchParams(filters);return this.request(`/api/market/quotes?${p}`);}
  createMarketQuote(data){return this.request("/api/market/quotes",{method:"POST",body:data});}
  recognizeMarketSheet(data){return this.request("/api/market/sheet/recognize",{method:"POST",body:data,timeout:300000});}
  importMarketSheet(data){return this.request("/api/market/sheet/import",{method:"POST",body:data,timeout:30000});}
  marketFeedStatus(){return this.request("/api/market/feed/status");}
  syncMarketFeed(pageId){return this.request("/api/market/feed/sync",{method:"POST",body:{pageId},timeout:300000});}
  deleteMarketQuote(id){return this.request(`/api/market/quotes/${encodeURIComponent(id)}/delete`,{method:"POST",body:{}});}
  marketSummary(filters){const p=new URLSearchParams(filters);return this.request(`/api/market/summary?${p}`);}
  pricingDecisions(filters={}){const p=new URLSearchParams(filters);return this.request(`/api/market/decisions?${p}`);}
  createPricingDecision(data){return this.request("/api/market/decisions",{method:"POST",body:data});}
  priceSuggestion(id){return this.request(`/api/devices/${encodeURIComponent(id)}/price-suggestion`);}
  salesCopy(id){return this.request(`/api/devices/${encodeURIComponent(id)}/sales-copy`);}
  addPhoto(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/photos`,{method:"POST",body:data});}
  importCsv(csv){return this.request("/api/import/devices.csv",{method:"POST",body:{csv}});}
  devices(query="",scope="today_intake"){const p=new URLSearchParams();if(query)p.set("q",query);if(scope)p.set("scope",scope);return this.request(`/api/devices?${p}`);}
  device(id){return this.request(`/api/devices/${encodeURIComponent(id)}`);}
  updateDevice(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/update`,{method:"POST",body:data});}
  changeStatus(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/status`,{method:"POST",body:data});}
  reserve(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/reserve`,{method:"POST",body:data});}
  cancelReservation(id){return this.request(`/api/devices/${encodeURIComponent(id)}/reservation/cancel`,{method:"POST",body:{}});}
  startRepair(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/repair/start`,{method:"POST",body:data});}
  completeRepair(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/repair/complete`,{method:"POST",body:data});}
  returnDevice(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/return`,{method:"POST",body:data});}
  recognizeQr(image){return this.request("/api/scan/recognize",{method:"POST",body:{image}});}
  recognizeScreenshot(image){return this.request("/api/devices/screenshot/recognize",{method:"POST",body:{image},timeout:60000});}
  appleConnectionStatus(){return this.request("/api/device-connect/apple/status",{timeout:10000});}
  readAppleDevice(udid=""){return this.request("/api/device-connect/apple/read",{method:"POST",body:{udid},timeout:45000});}
  intake(data){return this.request("/api/devices/intake",{method:"POST",body:data});}
  quickIntake(data){return this.request("/api/devices/quick-intake",{method:"POST",body:data});}
  printLabel(id){return this.request(`/api/devices/${encodeURIComponent(id)}/print`,{method:"POST",body:{}});}
  sell(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/sell`,{method:"POST",body:data,timeout:10000});}
  reissueHandoff(saleId){return this.request(`/api/sales/${encodeURIComponent(saleId)}/handoff/reissue`,{method:"POST",body:{}});}
  voidHandoff(saleId){return this.request(`/api/sales/${encodeURIComponent(saleId)}/handoff/void`,{method:"POST",body:{}});}
  events(){return this.request("/api/events");}
  alerts(){return this.request("/api/alerts");}
  auditEvents(filters={}){const p=new URLSearchParams(filters);return this.request(`/api/audit-events?${p}`);}
  createAfterSales(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/after-sales`,{method:"POST",body:data});}
  resolveAfterSales(id,data){return this.request(`/api/after-sales/${encodeURIComponent(id)}/resolve`,{method:"POST",body:data});}
  access(){return this.request("/api/access");}
}
