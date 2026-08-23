export interface BootstrapLocation {
  hash: string;
  pathname: string;
  search: string;
}

export interface HistoryReplacement {
  replaceState(data: unknown, unused: string, url?: string | URL | null): void;
}

export function consumeBootstrapCapability(location: BootstrapLocation, history: HistoryReplacement): string | null {
  const prefix = "#/setup/";
  if (!location.hash.startsWith(prefix)) return null;
  const candidate = location.hash.slice(prefix.length);
  history.replaceState(null, "", `${location.pathname}${location.search}#/setup`);
  return /^[A-Za-z0-9_-]{43}$/.test(candidate) ? candidate : null;
}

export async function submitBootstrapCapability<T>(
  capability: string,
  submit: (capability: string) => Promise<T>,
  onConsumed: () => void,
): Promise<T> {
  const result = await submit(capability);
  onConsumed();
  return result;
}

export function csrfCookieValue(cookieHeader: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0 || part.slice(0, separator).trim() !== "townlight_csrf_v1") continue;
    const value = part.slice(separator + 1).trim();
    return value || null;
  }
  return null;
}

export function authenticatedRequestInit(init: RequestInit = {}, cookieHeader = ""): RequestInit {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  let body = init.body;
  if (typeof body === "string" && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    try { body = JSON.stringify(withoutBrowserIdentity(JSON.parse(body))); }
    catch { /* Non-JSON string bodies pass through unchanged. */ }
  }
  if (typeof init.body === "string" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfCookieValue(cookieHeader);
    if (csrf) headers.set("X-TownLight-CSRF", csrf);
  }
  return { ...init, body, credentials: "same-origin", headers };
}

export function authGateForStatus(status: number): "login_required" | "bootstrap_required" | null {
  return status === 401 ? "login_required" : status === 428 ? "bootstrap_required" : null;
}

export function withoutBrowserIdentity<T>(value: T): T {
  const forbidden = new Set(["operator_id", "actor_id", "approved_by", "acknowledged_by", "updated_by", "requested_by", "submitted_by", "moderated_by", "created_by"]);
  const visit = (item: unknown, parentKey: string | null): unknown => {
    if (Array.isArray(item)) return item.map((entry) => visit(entry, parentKey));
    if (!item || typeof item !== "object") return item;
    if (parentKey === "details") return item;
    return Object.fromEntries(Object.entries(item).filter(([key]) => !forbidden.has(key)).map(([key, entry]) => [key, visit(entry, key)]));
  };
  return visit(value, null) as T;
}
