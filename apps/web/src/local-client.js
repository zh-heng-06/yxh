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
  recognizeMarketSheet(data){return this.request("/api/market/sheet/recognize",{method:"POST",body:data,timeout:180000});}
  importMarketSheet(data){return this.request("/api/market/sheet/import",{method:"POST",body:data,timeout:30000});}
  deleteMarketQuote(id){return this.request(`/api/market/quotes/${encodeURIComponent(id)}/delete`,{method:"POST",body:{}});}
  marketSummary(filters){const p=new URLSearchParams(filters);return this.request(`/api/market/summary?${p}`);}
  pricingDecisions(filters={}){const p=new URLSearchParams(filters);return this.request(`/api/market/decisions?${p}`);}
  createPricingDecision(data){return this.request("/api/market/decisions",{method:"POST",body:data});}
  smartSummary(){return this.request("/api/smart/daily-summary");}
  parseIntakeText(text){return this.request("/api/smart/parse-intake",{method:"POST",body:{text}});}
  priceSuggestion(id){return this.request(`/api/devices/${encodeURIComponent(id)}/price-suggestion`);}
  salesCopy(id){return this.request(`/api/devices/${encodeURIComponent(id)}/sales-copy`);}
  addPhoto(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/photos`,{method:"POST",body:data});}
  importCsv(csv){return this.request("/api/import/devices.csv",{method:"POST",body:{csv}});}
  devices(query="",status=""){const p=new URLSearchParams();if(query)p.set("q",query);if(status)p.set("status",status);return this.request(`/api/devices?${p}`);}
  device(id){return this.request(`/api/devices/${encodeURIComponent(id)}`);}
  updateDevice(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/update`,{method:"POST",body:data});}
  changeStatus(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/status`,{method:"POST",body:data});}
  reserve(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/reserve`,{method:"POST",body:data});}
  cancelReservation(id){return this.request(`/api/devices/${encodeURIComponent(id)}/reservation/cancel`,{method:"POST",body:{}});}
  startRepair(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/repair/start`,{method:"POST",body:data});}
  completeRepair(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/repair/complete`,{method:"POST",body:data});}
  returnDevice(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/return`,{method:"POST",body:data});}
  stocktake(){return this.request("/api/stocktakes/current");}
  startStocktake(data){return this.request("/api/stocktakes/start",{method:"POST",body:data});}
  scanStocktake(id,code){return this.request(`/api/stocktakes/${encodeURIComponent(id)}/scan`,{method:"POST",body:{code}});}
  completeStocktake(id){return this.request(`/api/stocktakes/${encodeURIComponent(id)}/complete`,{method:"POST",body:{}});}
  recognizeQr(image){return this.request("/api/scan/recognize",{method:"POST",body:{image}});}
  recognizeScreenshot(image){return this.request("/api/devices/screenshot/recognize",{method:"POST",body:{image},timeout:60000});}
  intake(data){return this.request("/api/devices/intake",{method:"POST",body:data});}
  printLabel(id){return this.request(`/api/devices/${encodeURIComponent(id)}/print`,{method:"POST",body:{}});}
  sell(id,data){return this.request(`/api/devices/${encodeURIComponent(id)}/sell`,{method:"POST",body:data,timeout:10000});}
  events(){return this.request("/api/events");}
  access(){return this.request("/api/access");}
}
