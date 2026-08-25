// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  canSubmitReceiverReceipt,
  receiverReceiptOutcomeLabel,
} from "./receiver-receipt-contract.ts";

test("receiver receipt requires bounded explicit external evidence", () => {
  const now = 1_777_000_000_000;
  assert.equal(canSubmitReceiverReceipt({
    sinkId: "sink-1",
    receiverLabel: "Cable master control",
    evidenceReference: "ticket-1042",
    detail: "Decoded video and audible program audio confirmed.",
    observedAtUnixMs: now - 1_000,
  }, now), true);
  assert.equal(canSubmitReceiverReceipt({
    sinkId: "",
    receiverLabel: "Cable master control",
    evidenceReference: "ticket-1042",
    detail: "Decoded video and audible program audio confirmed.",
    observedAtUnixMs: now - 1_000,
  }, now), false);
  assert.equal(canSubmitReceiverReceipt({
    sinkId: "sink-1",
    receiverLabel: "Cable master control",
    evidenceReference: "",
    detail: "Decoded video and audible program audio confirmed.",
    observedAtUnixMs: now - 1_000,
  }, now), false);
  assert.equal(canSubmitReceiverReceipt({
    sinkId: "sink-1",
    receiverLabel: "Cable master control",
    evidenceReference: "ticket-1042",
    detail: "Decoded video and audible program audio confirmed.",
    observedAtUnixMs: now + 1,
  }, now), false);
});

test("receipt outcome labels never imply TownLight created external proof", () => {
  assert.equal(receiverReceiptOutcomeLabel("accepted"), "Accepted by external receiver");
  assert.equal(receiverReceiptOutcomeLabel("rejected"), "Rejected by external receiver");
});
