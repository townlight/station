// @ts-nocheck
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  AGENDA_IMPORT_MAX_UTF8_BYTES,
  AGENDA_TITLE_MAX_UTF8_BYTES,
  agendaTextByteError,
  isCurrentAgendaRequest,
  utf8ByteLength,
} from "./agenda-contract.ts";

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

test("agenda UTF-8 guards accept exact ASCII boundaries and reject max plus one", () => {
  assert.equal(utf8ByteLength("a".repeat(AGENDA_TITLE_MAX_UTF8_BYTES)), 200);
  assert.equal(agendaTextByteError("a".repeat(200), AGENDA_TITLE_MAX_UTF8_BYTES, "Meeting title"), null);
  assert.match(agendaTextByteError("a".repeat(201), AGENDA_TITLE_MAX_UTF8_BYTES, "Meeting title") ?? "", /200 UTF-8 bytes/);
  assert.equal(utf8ByteLength("a".repeat(AGENDA_IMPORT_MAX_UTF8_BYTES)), 65_536);
  assert.equal(agendaTextByteError("a".repeat(65_536), AGENDA_IMPORT_MAX_UTF8_BYTES, "Import text"), null);
  assert.match(agendaTextByteError("a".repeat(65_537), AGENDA_IMPORT_MAX_UTF8_BYTES, "Import text") ?? "", /65,536 UTF-8 bytes/);
});

test("agenda UTF-8 guards measure astral and multibyte text instead of UTF-16 units", () => {
  const exactEmojiTitle = "😀".repeat(50);
  const overflowingEmojiTitle = `${exactEmojiTitle}😀`;
  assert.equal(exactEmojiTitle.length, 100);
  assert.equal(utf8ByteLength(exactEmojiTitle), 200);
  assert.equal(agendaTextByteError(exactEmojiTitle, AGENDA_TITLE_MAX_UTF8_BYTES, "Meeting title"), null);
  assert.equal(overflowingEmojiTitle.length, 102);
  assert.match(agendaTextByteError(overflowingEmojiTitle, AGENDA_TITLE_MAX_UTF8_BYTES, "Meeting title") ?? "", /204 UTF-8 bytes/);

  const exactImport = "é".repeat(32_768);
  assert.equal(exactImport.length, 32_768);
  assert.equal(utf8ByteLength(exactImport), 65_536);
  assert.equal(agendaTextByteError(exactImport, AGENDA_IMPORT_MAX_UTF8_BYTES, "Import text"), null);
  assert.match(agendaTextByteError(`${exactImport}é`, AGENDA_IMPORT_MAX_UTF8_BYTES, "Import text") ?? "", /65,538 UTF-8 bytes/);
});

test("agenda component treats maxLength as convenience and byte guards as authority", () => {
  assert.match(source, /titleByteError/);
  assert.match(source, /importByteError/);
  assert.match(source, /disabled=\{busy \|\| !channelId \|\| !!titleByteError\}/);
  assert.match(source, /disabled=\{busy \|\| !!visibleAgenda \|\| !importText\.trim\(\) \|\| !!titleByteError \|\| !!importByteError\}/);
});
