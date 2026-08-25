// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import { captionTrackMutationReason, parseCaptionFile } from "./caption-track-contract.ts";

test("caption import accepts reviewed WebVTT and SRT timing without trusting source cue identity", () => {
  const webvtt = parseCaptionFile(`WEBVTT

source-name
00:00:01.000 --> 00:00:03.250 align:start
Welcome to TownLight.

00:00:04.000 --> 00:00:05.500
Second line
continues here.
`);
  assert.equal(webvtt.length, 2);
  assert.equal(webvtt[0].start_ms, 1_000);
  assert.equal(webvtt[0].duration_ms, 2_250);
  assert.equal(webvtt[0].text, "Welcome to TownLight.");
  assert.match(webvtt[0].cue_id, /^[0-9a-f-]{36}$/);
  assert.equal(webvtt[1].text, "Second line\ncontinues here.");

  const srt = parseCaptionFile(`1
00:00:06,500 --> 00:00:08,000
Council is now in session.
`);
  assert.equal(srt.length, 1);
  assert.equal(srt[0].start_ms, 6_500);
  assert.equal(srt[0].duration_ms, 1_500);
});

test("caption import rejects malformed, overlapping, empty, and unbounded cue content", () => {
  assert.throws(() => parseCaptionFile("WEBVTT\n\nnot timing\ntext"), /timing/i);
  assert.throws(
    () => parseCaptionFile("WEBVTT\n\n00:00:02.000 --> 00:00:04.000\nA\n\n00:00:03.000 --> 00:00:05.000\nB"),
    /overlap/i,
  );
  assert.throws(() => parseCaptionFile("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"), /text/i);
  assert.throws(
    () => parseCaptionFile(`WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n${"x".repeat(2_049)}`),
    /2,048/,
  );
});

test("caption track mutation stays bounded to a ready asset and valid service metadata", () => {
  const cues = parseCaptionFile("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nReady");
  assert.match(captionTrackMutationReason(false, "en-US", 1, "Reviewed English", cues) ?? "", /ready/i);
  assert.match(captionTrackMutationReason(true, "bad language", 1, "Reviewed English", cues) ?? "", /language/i);
  assert.match(captionTrackMutationReason(true, "en-US", 0, "Reviewed English", cues) ?? "", /service/i);
  assert.match(captionTrackMutationReason(true, "en-US", 1, "", cues) ?? "", /label/i);
  assert.match(captionTrackMutationReason(true, "en-US", 1, "Reviewed English", []) ?? "", /cue/i);
  assert.equal(captionTrackMutationReason(true, "en-US", 1, "Reviewed English", cues), null);
});
