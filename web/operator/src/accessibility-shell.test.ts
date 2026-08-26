// @ts-nocheck
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const shell = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
const cg = readFileSync(new URL("./cg.tsx", import.meta.url), "utf8");
const metadata = readFileSync(new URL("./metadata.tsx", import.meta.url), "utf8");
const captionTracks = readFileSync(new URL("./caption-tracks.tsx", import.meta.url), "utf8");
const recordings = readFileSync(new URL("./recordings.tsx", import.meta.url), "utf8");
const commissioning = readFileSync(new URL("./commissioning.tsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("station shell exposes current-page semantics and moves focus with routed content", () => {
  assert.match(shell, /className="skip-link" href="#station-workspace"/);
  assert.match(shell, /<nav aria-label="Station screens">/);
  assert.match(shell, /aria-current=\{view === item\.id \? "page" : undefined\}/);
  assert.match(shell, /id="station-workspace" className="workspace" ref=\{workspaceRef\} tabIndex=\{-1\}/);
  assert.match(shell, /requestAnimationFrame\(\(\) => workspaceRef\.current\?\.focus\(\)\)/);
});

test("keyboard focus and mobile navigation targets remain visible and operable", () => {
  assert.match(styles, /button:focus-visible,[^{]+\{ outline:3px solid #78a988; outline-offset:3px; \}/);
  assert.match(styles, /@media \(max-width:760px\) \{ \.mobile-nav button \{ min-height:56px;/);
  assert.match(styles, /\.metric p,\.data-row small,\.job-row small,label small,\.asset-copy code,\.runtime-card>small,\.alert-card>small \{ color:var\(--muted\); \}/);
  assert.match(styles, /:root \{ --amber:#8a5a0d; \}/);
  assert.match(styles, /\.pill-active \{ color:#5d643f; \}/);
  assert.match(styles, /\.safe-yellow \.safe-indicator \{ color:#76500f; \}/);
  assert.match(styles, /\.cg-state-pill \{ color:#56645f; \}/);
  assert.match(styles, /\.cg-timeline code \{ color:#4f6c62; \}/);
  assert.match(styles, /\.commission-wizard-rail button>span \{ color:#4a6158; \}/);
  assert.match(styles, /\.commission-blocked \.commission-check-mark \{ color:#91332f; \}/);
});

test("S20 keeps dense CG controls reflowable and contrast-safe", () => {
  assert.match(cg, /className="page-header cg-operator-header"/);
  assert.match(styles, /\.cg-operator-header \.header-actions \{ display:grid; grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  assert.match(styles, /\.cg-operator-header \.cg-channel-picker select \{ min-width:0; \}/);
  assert.match(styles, /\.metric strong small \{ color:#5d746b; \}/);
  assert.match(styles, /\.cg-url \{ color:#4f6c62; \}/);
  assert.match(styles, /\.commission-evidence>div>span,\.stationbox-dimensions small \{ color:#4f6c62; \}/);
});

test("operator state changes expose stable assistive-technology semantics", () => {
  assert.match(cg, /role="tablist"/);
  assert.match(cg, /id=\{`cg-tab-\$\{item\.id\}`\}/);
  assert.match(cg, /role="tab" aria-selected=\{route === item\.id\} aria-controls="cg-tabpanel" tabIndex=\{route === item\.id \? 0 : -1\}/);
  assert.match(cg, /if \(event\.key === "ArrowRight"\)/);
  assert.match(cg, /if \(event\.key === "ArrowLeft"\)/);
  assert.match(cg, /if \(event\.key === "Home"\)/);
  assert.match(cg, /if \(event\.key === "End"\)/);
  assert.match(cg, /tabRefs\.current\[index\]\?\.focus\(\)/);
  assert.match(cg, /onNavigate\(CG_ROUTES\[index\]\.id\)/);
  assert.match(cg, /id="cg-tabpanel" role="tabpanel" aria-labelledby=\{`cg-tab-\$\{route\}`} tabIndex=\{0\}/);
  assert.match(metadata, /aria-pressed=\{schema\?\.schema_id === item\.schema_id\}/);
  assert.match(captionTracks, /aria-pressed=\{track\.track_id === trackId\}/);
  assert.match(recordings, /aria-pressed=\{selected\?\.definition\.recording_id === item\.definition\.recording_id\}/);
  assert.match(shell, /className="live-state-copy" role="status" aria-live="polite" aria-atomic="true"/);
  assert.match(shell, /<\/small><\/div>\{session\.state === "on_air" && <div className="live-duration">/);
  assert.match(shell, /role="dialog" aria-labelledby="live-confirm-take-title"/);
  assert.match(shell, /role="dialog" aria-labelledby="live-confirm-return-title"/);
  assert.doesNotMatch(shell, /role="alertdialog"/);
  assert.doesNotMatch(shell, /aria-modal="true"/);
  assert.match(shell, /session\.session_id !== confirmationSessionIdRef\.current \|\| session\.state !== expectedState \|\| !dialog/);
  assert.match(shell, /\[confirmAction, session\?\.session_id, session\?\.state\]/);
  assert.match(shell, /if \(!dialog\.contains\(document\.activeElement\)\) dialog\.focus\(\)/);
  assert.match(shell, /trigger\?\.isConnected && !trigger\.disabled/);
  assert.match(shell, /confirmationFallbackRef\.current\?\.focus\(\)/);
  assert.match(shell, /onKeyDown=\{\(event\) => \{ if \(event\.key === "Escape"\) setConfirmAction\(null\); \}\}/);
  assert.match(shell, /role=\{session\.state === "failed" \? "alert" : "status"\}/);
  assert.match(cg, /role="alert"/);
  assert.match(cg, /role="status"/);
  assert.match(cg, /cg-preview-status" role="status" aria-live="polite"/);
  assert.match(cg, /cg-preview-status cg-preview-status-error" role="alert"/);
  assert.match(shell, /<div className="notice" role="status">/);
  assert.match(shell, /formError && <div className="error-box" role="alert">/);
  assert.match(shell, /supportError && <div className="error-box" role="alert">/);
  assert.match(shell, /retention_warning && <div className="error-box" role="alert">/);
  assert.match(shell, /updateError && <div className="error-box" role="alert">/);
  assert.doesNotMatch(shell, /onAction=\{\(\) => undefined\}/);
  assert.doesNotMatch(shell, /<div className="notice">/);
  assert.doesNotMatch(shell, /<div className="error-box">\{(?:error|formError|supportError|updateError)\}/);
  assert.match(commissioning, /aria-controls=\{.*commission-evidence/);
  assert.match(commissioning, /id=\{.*commission-evidence/);
  assert.match(commissioning, /commission-evidence-\$\{checkIndex\}/);
});
