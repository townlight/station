// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  commissioningAction,
  commissioningGroups,
  commissioningStatusLabel,
  commissioningWizardCoverage,
  commissioningWizardSteps,
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

test("the 11-screen wizard covers every schema-v2 authoritative check exactly once", () => {
  const ids = [
    "station_profile", "operator_account_auth", "runtime_api", "station_box_storage", "station_time", "station_backup",
    "channel_watch_folder", "fallback_slate", "cea708_captions", "publish_targets", "automation_model_policy",
    "headend_output_setup", "cable_transport_validation", "guided_ingest", "guided_schedule", "guided_air_proof",
    "selected_sink_ten_minute_proof", "support_bundle", "first_broadcast_report", "external_receiver_proof",
    "clean_machine_install_proof", "one_station_week_proof", "three_station_week_proof",
  ];
  const report = {
    schema_version: 2,
    overall_status: "blocked",
    current_step_id: "cable_transport_validation",
    checks: ids.map((id) => ({ id, label: id, status: id === "station_profile" ? "pass" : "blocked", blocking: true, detail: id, evidence: id })),
  } as AuthoritativeCommissioningReport;

  const steps = commissioningWizardSteps(report);
  assert.equal(steps.length, 11);
  assert.deepEqual(commissioningWizardCoverage(report), { unmapped: [], duplicated: [], missing: [] });
  assert.deepEqual(steps.flatMap((step) => step.checks.map((check) => check.id)).sort(), [...ids].sort());
  assert.equal(steps.find((step) => step.current)?.id, "cable_checks");
  assert.equal(steps[0].status, "pass");
});

test("new or multiply assigned server checks fail visibly instead of disappearing", () => {
  const report = {
    overall_status: "blocked",
    checks: [{ id: "future_required_check", label: "Future", status: "blocked", blocking: true, detail: "Missing", evidence: "Server" }],
  } as AuthoritativeCommissioningReport;
  const coverage = commissioningWizardCoverage(report);
  assert.deepEqual(coverage.unmapped, ["future_required_check"]);
  assert.equal(coverage.duplicated.length, 0);
  assert.ok(coverage.missing.includes("station_profile"));
  assert.equal(commissioningWizardSteps(report)[0].status, "unknown");

  const duplicate = { ...report, checks: [report.checks[0], report.checks[0]] };
  assert.deepEqual(commissioningWizardCoverage(duplicate).duplicated, ["future_required_check"]);

  const duplicateMapped = { ...report, checks: [
    { id: "station_profile", label: "Profile A", status: "pass", blocking: false, detail: "A", evidence: "A" },
    { id: "station_profile", label: "Profile B", status: "pass", blocking: false, detail: "B", evidence: "B" },
  ] } as AuthoritativeCommissioningReport;
  assert.deepEqual(commissioningWizardCoverage(duplicateMapped).duplicated, ["station_profile"]);
  assert.equal(commissioningWizardSteps(duplicateMapped)[0].checks.length, 2);
  assert.equal(commissioningWizardSteps(duplicateMapped)[0].status, "unknown");
});
