import { authenticatedRequestInit, authGateForStatus } from "./auth-contract";

export type AuthGate = "login_required" | "bootstrap_required";
export const AUTH_GATE_EVENT = "townlight-auth-gate";

export class ApiError extends Error {
  constructor(public status: number, message: string, public code: string | null = null) {
    super(message);
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, authenticatedRequestInit(init, document.cookie));
  const body = await response.json().catch(() => null) as { error?: string | { code?: string; message?: string }; detail?: string; message?: string } | T | null;
  if (!response.ok) {
    const gate = authGateForStatus(response.status);
    if (gate) window.dispatchEvent(new CustomEvent<AuthGate>(AUTH_GATE_EVENT, { detail: gate }));
    const errorBody = body && typeof body === "object" ? body as { error?: string | { code?: string; message?: string }; detail?: string; message?: string } : null;
    const nested = errorBody?.error && typeof errorBody.error === "object" ? errorBody.error : null;
    const flatCode = typeof errorBody?.error === "string" ? errorBody.error : null;
    throw new ApiError(response.status, errorBody?.detail ?? errorBody?.message ?? nested?.message ?? `Request failed with HTTP ${response.status}.`, nested?.code ?? flatCode);
  }
  return body as T;
}
