const SESSION_KEY = "zhanggui_cloud_session_v1";

export class SupabaseClient {
  constructor({ supabaseUrl, supabaseAnonKey }) {
    this.url = String(supabaseUrl || "").replace(/\/$/, "");
    this.anonKey = supabaseAnonKey || "";
    this.session = this.#loadSession();
    if (!this.url || !this.anonKey) throw new Error("云端地址尚未配置");
  }

  get user() { return this.session?.user || null; }
  get accessToken() { return this.session?.access_token || null; }

  async signIn(email, password) {
    const response = await this.#request("/auth/v1/token?grant_type=password", {
      method: "POST",
      auth: false,
      body: { email, password }
    });
    this.#saveSession(response);
    return response.user;
  }

  async signOut() {
    if (this.accessToken) {
      await this.#request("/auth/v1/logout", { method: "POST" }).catch(() => {});
    }
    this.session = null;
    localStorage.removeItem(SESSION_KEY);
  }

  async refreshSession() {
    if (!this.session?.refresh_token) throw new Error("登录已失效");
    const response = await this.#request("/auth/v1/token?grant_type=refresh_token", {
      method: "POST",
      auth: false,
      body: { refresh_token: this.session.refresh_token }
    });
    this.#saveSession(response);
    return response;
  }

  async rest(path, options = {}) {
    return this.#request(`/rest/v1/${path}`, options, true);
  }

  async rpc(functionName, args = {}) {
    return this.rest(`rpc/${functionName}`, { method: "POST", body: args });
  }

  async upload(bucket, path, file) {
    const response = await fetch(`${this.url}/storage/v1/object/${bucket}/${path}`, {
      method: "POST",
      headers: this.#headers({ "Content-Type": file.type || "application/octet-stream", "x-upsert": "false" }),
      body: file
    });
    if (!response.ok) throw await this.#error(response);
    return response.json();
  }

  #headers(extra = {}, auth = true) {
    const headers = { apikey: this.anonKey, ...extra };
    if (auth && this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`;
    return headers;
  }

  async #request(path, options = {}, retryAuth = false) {
    const { method = "GET", body, auth = true, headers = {} } = options;
    const response = await fetch(`${this.url}${path}`, {
      method,
      headers: this.#headers({ ...(body ? { "Content-Type": "application/json" } : {}), ...headers }, auth),
      body: body ? JSON.stringify(body) : undefined
    });
    if (response.status === 401 && retryAuth && this.session?.refresh_token) {
      await this.refreshSession();
      return this.#request(path, options, false);
    }
    if (!response.ok) throw await this.#error(response);
    if (response.status === 204) return null;
    const text = await response.text();
    return text ? JSON.parse(text) : null;
  }

  async #error(response) {
    let detail = "";
    try { detail = (await response.json()).message || ""; } catch { detail = await response.text().catch(() => ""); }
    const error = new Error(detail || `云端请求失败 (${response.status})`);
    error.status = response.status;
    return error;
  }

  #loadSession() {
    try { return JSON.parse(localStorage.getItem(SESSION_KEY)); } catch { return null; }
  }

  #saveSession(session) {
    this.session = session;
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  }
}
