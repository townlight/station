import { FormEvent, ReactNode, useCallback, useEffect, useState } from "react";
import { AUTH_GATE_EVENT, ApiError, AuthGate, apiRequest } from "./api-client";
import { consumeBootstrapCapability, retainRecoveryCodesBeforeSessionVerification, submitBootstrapCapability } from "./auth-contract";

interface Session {
  account_id: string;
  username: string;
  display_name: string;
  roles: string[];
  absolute_expires_at_unix_ms: number;
}

interface AuthStatus {
  schema_version: 1;
  state: "bootstrap_required" | "login_required" | "authenticated";
  session?: Session;
}

let initialBootstrapCapability = consumeBootstrapCapability(window.location, window.history);

export function AuthBoundary({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<AuthGate | "checking">("checking");
  const [session, setSession] = useState<Session | null>(null);
  const [capability, setCapability] = useState(() => initialBootstrapCapability);
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [savedCodes, setSavedCodes] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const verifySession = useCallback(async () => {
    const verified = await apiRequest<{ session: Session }>("/api/v1/auth/session");
    setSession(verified.session);
    setGate("checking");
    return verified.session;
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      const status = await apiRequest<AuthStatus>("/api/v1/auth/status");
      if (status.state === "authenticated") await verifySession();
      else { setSession(null); setGate(status.state); }
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 428) setGate("bootstrap_required");
      else if (problem instanceof ApiError && problem.status === 401) setGate("login_required");
      else { setNotice(problem instanceof Error ? problem.message : "Authentication status could not be read."); setGate("login_required"); }
    }
  }, [verifySession]);

  useEffect(() => { void loadStatus(); }, [loadStatus]);
  useEffect(() => {
    const handler = (event: Event) => {
      const next = (event as CustomEvent<AuthGate>).detail;
      setSession(null); setGate(next);
      if (next === "bootstrap_required") void loadStatus();
    };
    window.addEventListener(AUTH_GATE_EVENT, handler);
    return () => window.removeEventListener(AUTH_GATE_EVENT, handler);
  }, [loadStatus]);

  async function bootstrap(username: string, displayName: string, password: string) {
    if (!capability) throw new Error("This setup link has no valid bootstrap capability. Reopen the installer-provided link.");
    const created = await submitBootstrapCapability(capability, (submittedCapability) =>
      apiRequest<{ session: Session; recovery_codes: string[] }>("/api/v1/auth/bootstrap", { method: "POST", body: JSON.stringify({ capability: submittedCapability, username, display_name: displayName, password }) }), () => {
      initialBootstrapCapability = null;
      setCapability(null);
    });
    setSavedCodes(false);
    try {
      await retainRecoveryCodesBeforeSessionVerification(created.recovery_codes, setRecoveryCodes, verifySession);
    } catch {
      setSession(null);
      setGate("login_required");
      setNotice("The account was created, but station access could not be confirmed. Save these one-time recovery codes, then sign in again.");
    }
  }

  async function login(username: string, password: string) {
    await apiRequest("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    await verifySession();
  }

  async function recover(username: string, recoveryCode: string, password: string) {
    await apiRequest("/api/v1/auth/recover", { method: "POST", body: JSON.stringify({ username, recovery_code: recoveryCode, new_password: password }) });
    setSession(null); setGate("login_required"); setNotice("Password recovered. Sign in with the new password.");
  }

  async function logout() {
    await apiRequest("/api/v1/auth/logout", { method: "POST", body: "{}" });
    setSession(null); setGate("login_required"); setNotice("Signed out.");
  }

  async function changePassword(currentPassword: string, newPassword: string) {
    await apiRequest("/api/v1/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
    setShowPassword(false); setSession(null); setGate("login_required"); setNotice("Password changed. Sign in again.");
  }

  if (gate === "checking" && !session) return <div className="auth-boot"><div className="mark">TL</div><div className="boot-line"><i /></div><p>Verifying operator session…</p></div>;
  if (recoveryCodes) return <RecoveryCodes codes={recoveryCodes} saved={savedCodes} sessionConfirmed={Boolean(session)} onSaved={setSavedCodes} onContinue={() => setRecoveryCodes(null)} />;
  if (!session) return <AuthGateScreen gate={gate === "checking" ? "login_required" : gate} capabilityAvailable={Boolean(capability)} notice={notice} onBootstrap={bootstrap} onLogin={login} onRecover={recover} />;

  return <div className="authenticated-shell"><div className="auth-session-bar"><div><strong>{session.display_name}</strong><span>@{session.username} · {session.roles.join(", ")}</span></div><div><button className="text-button" onClick={() => setShowPassword((value) => !value)}>Change password</button><button className="secondary" onClick={() => void logout()}>Sign out</button></div></div>{showPassword && <PasswordPanel onCancel={() => setShowPassword(false)} onSubmit={changePassword} />}{children}</div>;
}

function AuthGateScreen({ gate, capabilityAvailable, notice, onBootstrap, onLogin, onRecover }: { gate: AuthGate; capabilityAvailable: boolean; notice: string | null; onBootstrap: (username: string, displayName: string, password: string) => Promise<void>; onLogin: (username: string, password: string) => Promise<void>; onRecover: (username: string, code: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState(""); const [displayName, setDisplayName] = useState(""); const [password, setPassword] = useState(""); const [confirm, setConfirm] = useState(""); const [code, setCode] = useState(""); const [recovering, setRecovering] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  async function submit(event: FormEvent) { event.preventDefault(); if (password !== confirm && (gate === "bootstrap_required" || recovering)) { setError("Passwords do not match."); return; } setBusy(true); setError(null); try { if (gate === "bootstrap_required") await onBootstrap(username.trim(), displayName.trim(), password); else if (recovering) await onRecover(username.trim(), code.trim(), password); else await onLogin(username.trim(), password); } catch (problem) { setError(problem instanceof Error ? problem.message : "Authentication failed."); } finally { setBusy(false); } }
  return <main className="auth-shell"><section className="auth-brand"><div className="mark">TL</div><p className="eyebrow">TownLight Station</p><h1>{gate === "bootstrap_required" ? "Create the first operator account." : recovering ? "Recover operator access." : "Operator sign in."}</h1><p>Station controls are available only through an authenticated, same-origin session.</p></section><section className="auth-card"><p className="eyebrow">{gate === "bootstrap_required" ? "One-time station setup" : recovering ? "One-time recovery code" : "Local operator account"}</p><h2>{gate === "bootstrap_required" ? "Secure this station." : recovering ? "Set a new password." : "Welcome back."}</h2>{notice && <div className="notice" role="status">{notice}</div>}{gate === "bootstrap_required" && !capabilityAvailable ? <div className="error-box" role="alert">No valid bootstrap capability is present. Reopen the one-time setup link provided by the installer; this screen cannot create or recover it.</div> : <form onSubmit={submit}><label>Username<input autoFocus required minLength={3} maxLength={64} autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} /></label>{gate === "bootstrap_required" && <label>Display name<input required minLength={2} maxLength={120} autoComplete="name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>}{recovering && <label>Recovery code<input required autoComplete="one-time-code" value={code} onChange={(event) => setCode(event.target.value)} /></label>}<label>{recovering ? "New password" : "Password"}<input required minLength={12} type="password" autoComplete={gate === "login_required" && !recovering ? "current-password" : "new-password"} value={password} onChange={(event) => setPassword(event.target.value)} /></label>{(gate === "bootstrap_required" || recovering) && <label>Confirm password<input required minLength={12} type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} /></label>}{error && <div className="error-box" role="alert">{error}</div>}<button className="primary wide" disabled={busy}>{busy ? "Verifying…" : gate === "bootstrap_required" ? "Create account securely" : recovering ? "Recover account" : "Sign in"}</button></form>}{gate === "login_required" && <button className="text-button auth-recover-link" onClick={() => { setRecovering((value) => !value); setError(null); setPassword(""); setConfirm(""); }}>{recovering ? "Return to sign in" : "Use a recovery code"}</button>}</section></main>;
}

function RecoveryCodes({ codes, saved, sessionConfirmed, onSaved, onContinue }: { codes: string[]; saved: boolean; sessionConfirmed: boolean; onSaved: (saved: boolean) => void; onContinue: () => void }) {
  function download() { const url = URL.createObjectURL(new Blob([`TownLight Station recovery codes\n\n${codes.join("\n")}\n`], { type: "text/plain" })); const link = document.createElement("a"); link.href = url; link.download = "townlight-station-recovery-codes.txt"; link.click(); URL.revokeObjectURL(url); }
  return <main className="auth-shell"><section className="auth-brand"><div className="mark">TL</div><p className="eyebrow">Account created</p><h1>Save these recovery codes now.</h1><p>They are shown from this one response only. TownLight cannot display them again.</p></section><section className="auth-card recovery-card"><div className="recovery-warning" role="alert">Leaving this screen discards this copy. Each code is single-use.</div>{!sessionConfirmed && <div className="notice" role="status">Station access is not confirmed yet. Your recovery codes remain available; after saving them, sign in again.</div>}<ol>{codes.map((code) => <li key={code}><code>{code}</code></li>)}</ol><button className="secondary wide" onClick={download}>Download recovery codes</button><label className="auth-ack"><input type="checkbox" checked={saved} onChange={(event) => onSaved(event.target.checked)} /><span>I saved the recovery codes somewhere secure.</span></label><button className="primary wide" disabled={!saved} onClick={onContinue}>{sessionConfirmed ? "Continue to station" : "Continue to sign in"}</button></section></main>;
}

function PasswordPanel({ onCancel, onSubmit }: { onCancel: () => void; onSubmit: (currentPassword: string, newPassword: string) => Promise<void> }) {
  const [current, setCurrent] = useState(""); const [next, setNext] = useState(""); const [confirm, setConfirm] = useState(""); const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); if (next !== confirm) { setError("New passwords do not match."); return; } setBusy(true); setError(null); try { await onSubmit(current, next); } catch (problem) { setError(problem instanceof Error ? problem.message : "Password could not be changed."); setBusy(false); } }
  return <section className="auth-password-panel" aria-label="Change password"><form onSubmit={submit}><label>Current password<input autoFocus required type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} /></label><label>New password<input required minLength={12} type="password" autoComplete="new-password" value={next} onChange={(event) => setNext(event.target.value)} /></label><label>Confirm new password<input required minLength={12} type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} /></label>{error && <div className="error-box" role="alert">{error}</div>}<div className="button-row"><button type="button" className="secondary" onClick={onCancel}>Cancel</button><button className="primary" disabled={busy}>{busy ? "Changing…" : "Change password"}</button></div></form></section>;
}
