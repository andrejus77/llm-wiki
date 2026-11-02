// Tiny API client. Every call goes to the Django Ninja API under /api (OpenAPI docs at /api/docs).
export async function get<T = any>(path: string, params?: Record<string, any>, signal?: AbortSignal): Promise<T> {
  const qs = params
    ? "?" + Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== null && v !== "" && !(Array.isArray(v) && !v.length))
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(Array.isArray(v) ? v.join(",") : String(v))}`)
        .join("&")
    : "";
  const r = await fetch(`/api${path}${qs}`, { credentials: "same-origin", signal });
  if (r.status === 401) { location.href = "/accounts/login/?next=" + encodeURIComponent(location.pathname); throw new Error("login required"); }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function send<T = any>(method: string, path: string, body?: any): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method, credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) {
    const t = await r.text();
    let detail = t;
    try { detail = JSON.parse(t).detail || t; } catch { /* not JSON */ }
    throw new Error(String(detail).slice(0, 500));
  }
  return r.json();
}
