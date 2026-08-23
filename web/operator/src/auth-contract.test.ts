// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  authenticatedRequestInit,
  authGateForResponse,
  authGateForStatus,
  consumeBootstrapCapability,
  csrfCookieValue,
  submitBootstrapCapability,
  retainRecoveryCodesBeforeSessionVerification,
  withoutBrowserIdentity,
} from "./auth-contract.ts";

test("bootstrap capability is captured once from only the canonical fragment and immediately cleared", () => {
  const replacements: unknown[][] = [];
  const capability = "A".repeat(43);
  const value = consumeBootstrapCapability(
    { hash: `#/setup/${capability}`, pathname: "/", search: "?view=commissioning" },
    { replaceState: (...args: unknown[]) => replacements.push(args) },
  );
  assert.equal(value, capability);
  assert.deepEqual(replacements, [[null, "", "/?view=commissioning#/setup"]]);

  assert.equal(consumeBootstrapCapability(
    { hash: `#bootstrap=${capability}`, pathname: "/", search: "" },
    { replaceState: () => assert.fail("noncanonical fragments must not be rewritten") },
  ), null);
});

test("authenticated mutation DTOs remove browser identity at every nesting level", () => {
  assert.deepEqual(withoutBrowserIdentity({
    channel_id: "channel",
    operator_id: "forged",
    audit: { event_kind: "changed", operator_id: "forged", details: { approved_by: "content, not an authority field" } },
    bulletin: { title: "Notice", submitted_by: "forged", moderated_by: "forged" },
    approval: { item_id: "item", approved_by: "forged" },
  }), {
    channel_id: "channel",
    audit: { event_kind: "changed", details: { approved_by: "content, not an authority field" } },
    bulletin: { title: "Notice" },
    approval: { item_id: "item" },
  });
});

test("malformed setup fragment is cleared but never exposed as a capability", () => {
  let replacement = "";
  assert.equal(consumeBootstrapCapability(
    { hash: "#/setup/not%20safe", pathname: "/console", search: "" },
    { replaceState: (_data, _unused, url) => { replacement = String(url); } },
  ), null);
  assert.equal(replacement, "/console#/setup");
});

test("bootstrap capability remains available after a failed submission and clears only after success", async () => {
  const capability = "R".repeat(43);
  let available: string | null = capability;
  await assert.rejects(
    submitBootstrapCapability(capability, async () => { throw new Error("temporary network failure"); }, () => { available = null; }),
    /temporary network failure/,
  );
  assert.equal(available, capability);

  const result = await submitBootstrapCapability(capability, async (submitted) => {
    assert.equal(submitted, capability);
    return "created";
  }, () => { available = null; });
  assert.equal(result, "created");
  assert.equal(available, null);
});

test("state-changing requests carry same-origin cookies and readable CSRF token", () => {
  assert.equal(csrfCookieValue("a=1; townlight_csrf_v1=proof-token_123; b=2"), "proof-token_123");
  const post = authenticatedRequestInit({ method: "POST", body: "{}" }, "townlight_csrf_v1=proof-token_123");
  assert.equal(post.credentials, "same-origin");
  assert.equal(new Headers(post.headers).get("X-TownLight-CSRF"), "proof-token_123");
  assert.equal(new Headers(post.headers).get("Content-Type"), "application/json");
  const get = authenticatedRequestInit({}, "townlight_csrf_v1=proof-token_123");
  assert.equal(new Headers(get.headers).has("X-TownLight-CSRF"), false);
});

test("401 and 428 responses transition to the correct fail-closed gate", () => {
  assert.equal(authGateForStatus(401), "login_required");
  assert.equal(authGateForStatus(428), "bootstrap_required");
  assert.equal(authGateForStatus(403), null);
});

test("public credential submissions keep the active form on a 401 while protected requests still gate", () => {
  assert.equal(authGateForResponse("/api/v1/auth/login", 401), null);
  assert.equal(authGateForResponse("/api/v1/auth/recover", 401), null);
  assert.equal(authGateForResponse("/api/v1/auth/bootstrap", 401), null);
  assert.equal(authGateForResponse("/api/v1/auth/session", 401), "login_required");
  assert.equal(authGateForResponse("/api/v1/channels", 401), "login_required");
  assert.equal(authGateForResponse("/api/v1/auth/login", 428), "bootstrap_required");
});

test("bootstrap recovery codes are retained before session confirmation and survive its failure", async () => {
  const events: string[] = [];
  const codes = ["one-time-a", "one-time-b"];
  await assert.rejects(
    retainRecoveryCodesBeforeSessionVerification(codes, (retained) => {
      assert.deepEqual(retained, codes);
      events.push("codes-retained");
    }, async () => {
      events.push("session-verification");
      throw new Error("transient session confirmation failure");
    }),
    /transient session confirmation failure/,
  );
  assert.deepEqual(events, ["codes-retained", "session-verification"]);
});
