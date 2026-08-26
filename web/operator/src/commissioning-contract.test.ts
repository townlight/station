// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  commissioningAction,
  commissioningGroups,
  commissioningStatusLabel,
  currentCommissioningCheck,
  isCoreCommissioningComplete,
  isCommissioningComplete,
  isExternalCommissioningCheck,
} from "./commissioning-contract.ts";
import type { AuthoritativeCommissioningReport } from "./commissioning-contract.ts";

test("completion comes only from the authoritative report rollup", () => {
  const report: AuthoritativeCommissioningReport = {
    overall_status: "pass",
    checks: [
      {
        id: "installed_field_proof",
        label: "Installed and field proof",
        status: "not_verified",
        blocking: false,
        detail: "External field duration is not recorded locally.",
        evidence: "No local field-proof record",
        boundary: "external",
      },
    ],
  };

  assert.equal(isCommissioningComplete(report), true);
});

test("core completion is never inferred by the browser", () => {
  const legacy: AuthoritativeCommissioningReport = {
    overall_status: "warning",
    checks: [{ id: "station_profile", label: "Profile", status: "pass", blocking: false, detail: "Saved", evidence: "SQLite" }],
  };
  assert.equal(isCoreCommissioningComplete(legacy), false);
  assert.equal(isCoreCommissioningComplete({ ...legacy, schema_version: 2, core_complete: true }), true);
});

test("server step and order remain authoritative across phase grouping", () => {
  const report: AuthoritativeCommissioningReport = {
    schema_version: 2,
    overall_status: "blocked",
    current_step_id: "watch_folder",
    core_progress: { passed: 2, total: 4 },
    core_complete: false,
    checks: [
      { id: "station_profile", label: "Profile", phase: "identity", status: "pass", required: true, blocking: false, action: "station", detail: "Saved", evidence: "SQLite", boundary: "local_authority" },
      { id: "station_time", label: "Time", phase: "station_box", status: "unknown", required: true, blocking: true, action: "health", detail: "Unknown", evidence: "Windows probe", boundary: "local_authority" },
      { id: "watch_folder", label: "Watch folder", phase: "sources_outputs", status: "blocked", required: true, blocking: true, action: "media", detail: "Missing", evidence: "Configuration", boundary: "local_authority" },
      { id: "installed_field_proof", label: "Field proof", phase: "external_proof", status: "not_verified", required: false, blocking: false, action: null, detail: "External", evidence: "No record", boundary: "external_evidence" },
    ],
  };

  assert.equal(currentCommissioningCheck(report)?.id, "watch_folder");
  assert.deepEqual(commissioningGroups(report).flatMap((group) => group.checks.map((check) => check.id)), report.checks.map((check) => check.id));
});

test("actions and proof boundaries stay explicit", () => {
  const legacyHeadend = { id: "headend_output_setup", label: "Headend", status: "blocked", blocking: true, detail: "Missing", evidence: "Channel authority" } as const;
  const publishTargets = { id: "publish_targets", label: "Publish targets", status: "blocked", blocking: true, detail: "Missing", evidence: "Publishing authority" } as const;
  const external = { ...legacyHeadend, id: "external_receiver_proof" };
  assert.equal(commissioningAction(legacyHeadend), "channels");
  assert.equal(commissioningAction(publishTargets), "publishing");
  assert.equal(isExternalCommissioningCheck(external), true);
  assert.equal(commissioningStatusLabel("unknown"), "Unknown — verification required");
  assert.equal(commissioningStatusLabel("not_verified"), "Not verified");
});

test("schema-v2 explicit null action is not replaced by a browser fallback", () => {
  const blocked = { id: "fallback_slate", label: "Fallback", status: "blocked", blocking: true, action: null, detail: "Not implemented", evidence: "Authority" } as const;
  assert.equal(commissioningAction(blocked), null);
});
