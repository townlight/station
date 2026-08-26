// @ts-nocheck
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const shell = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
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
