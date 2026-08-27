// @ts-nocheck
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { isCurrentAgendaRequest } from "./agenda-contract.ts";

const source = readFileSync(new URL("./agenda.tsx", import.meta.url), "utf8");

test("agenda UI exposes bounded import and all operator states", () => {
  assert.match(source, /import-text/);
  assert.match(source, /maxLength=\{65536\}/);
  assert.match(source, /source hash/);
  assert.match(source, /role="status"/);
  assert.match(source, /role="alert"/);
  assert.match(source, /History/);
  assert.match(source, /PDF files are rejected/);
  assert.match(source, /text import is creation-only/);
  assert.match(source, /disabled=\{busy \|\| !!visibleAgenda/);
});

test("agenda UI invalidates stale identity and channel responses", () => {
  assert.match(source, /requestGeneration/);
  assert.match(source, /isCurrentAgendaRequest/);
  assert.match(source, /generation !== requestGeneration\.current/);
  assert.match(source, /setAgenda\(null\)/);
  assert.match(source, /agenda\.channel_id !== channelId/);
});

test("agenda request tokens reject stale generation, identity, and channel", () => {
  const token = { generation: 4, agendaId: "agenda-a", channelId: "channel-a" };
  assert.equal(isCurrentAgendaRequest(token, 4, "channel-a", "agenda-a", "channel-a"), true);
  assert.equal(isCurrentAgendaRequest(token, 5, "channel-a", "agenda-a", "channel-a"), false);
  assert.equal(isCurrentAgendaRequest(token, 4, "channel-b", "agenda-a", "channel-a"), false);
  assert.equal(isCurrentAgendaRequest(token, 4, "channel-a", "agenda-b", "channel-a"), false);
});
