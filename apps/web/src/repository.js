export class StoreRepository {
  constructor(client) {
    this.client = client;
    this.membership = null;
  }

  async loadMembership() {
    const userId = this.client.user?.id;
    if (!userId) throw new Error("请先登录");
    const rows = await this.client.rest(
      `shop_members?select=shop_id,role,shops(name,timezone)&user_id=eq.${encodeURIComponent(userId)}&active=eq.true&limit=1`
    );
    if (!rows?.length) throw new Error("该账号尚未加入门店");
    this.membership = rows[0];
    return this.membership;
  }

  get shopId() {
    if (!this.membership?.shop_id) throw new Error("门店信息尚未加载");
    return this.membership.shop_id;
  }

  async listDevices({ status, query, limit = 100 } = {}) {
    const filters = [
      "select=*",
      `shop_id=eq.${this.shopId}`,
      "deleted_at=is.null",
      "order=created_at.desc",
      `limit=${Math.min(limit, 500)}`
    ];
    if (status) filters.push(`status=eq.${encodeURIComponent(status)}`);
    if (query) {
      const safe = String(query).replace(/[(),]/g, "").slice(0, 50);
      filters.push(`or=(stock_code.ilike.*${safe}*,model.ilike.*${safe}*,imei.ilike.*${safe}*,serial_number.ilike.*${safe}*)`);
    }
    return this.client.rest(`devices?${filters.join("&")}`);
  }

  async getOwnerFinancials(deviceIds) {
    if (this.membership?.role !== "owner" || !deviceIds.length) return [];
    return this.client.rest(`device_financials?select=*&device_id=in.(${deviceIds.join(",")})`);
  }

  async uploadSnapshot(file) {
    const extension = file.name?.split(".").pop()?.replace(/[^a-z0-9]/gi, "") || "jpg";
    const path = `${this.shopId}/${new Date().toISOString().slice(0, 10)}/${crypto.randomUUID()}.${extension}`;
    await this.client.upload("device-snapshots", path, file);
    return path;
  }

  async intakeDevice({ device, purchaseCost, listPrice, minimumPrice = null, queueLabel = true }) {
    return this.client.rpc("intake_device", {
      device_data: { ...device, shop_id: this.shopId },
      purchase_cost: purchaseCost,
      list_price: listPrice,
      minimum_price: minimumPrice,
      queue_label: queueLabel
    });
  }

  async sellDevice(deviceId, salePrice, paymentMethod = "", customerNote = "") {
    return this.client.rpc("sell_device", {
      target_device: deviceId,
      final_price: salePrice,
      pay_method: paymentMethod,
      customer_text: customerNote
    });
  }

  async recentEvents(limit = 50) {
    return this.client.rest(
      `inventory_events?select=*,devices(stock_code,model,storage)&shop_id=eq.${this.shopId}&order=created_at.desc&limit=${Math.min(limit, 200)}`
    );
  }

  async dashboard() {
    const devices = await this.listDevices({ limit: 500 });
    const active = devices.filter(item => ["in_stock", "reserved", "sold_pending_pickup"].includes(item.status));
    const financials = await this.getOwnerFinancials(active.map(item => item.id));
    const costById = new Map(financials.map(row => [row.device_id, Number(row.purchase_cost)]));
    return {
      activeCount: active.length,
      inventoryCost: active.reduce((sum, item) => sum + (costById.get(item.id) || 0), 0),
      agedCount: active.filter(item => Date.now() - new Date(item.created_at).getTime() > 30 * 86400000).length,
      role: this.membership?.role
    };
  }
}
