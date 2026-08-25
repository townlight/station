export type MetadataFieldType =
  | { kind: "text"; constraints: { min_chars: number; max_chars: number } }
  | { kind: "number"; constraints: { minimum: number; maximum: number } }
  | { kind: "date_time"; constraints: { minimum_unix_ms: number | null; maximum_unix_ms: number | null } }
  // Rust's unit enum variant serializes as {"kind":"boolean"}; it has no
  // constraints member, and deny_unknown_fields rejects one if supplied.
  | { kind: "boolean" }
  | { kind: "controlled_choice"; constraints: { choices: ControlledChoice[] } };

export interface ControlledChoice { choice_id: string; key: string; label: string; }
export interface MetadataFieldDefinition {
  field_id: string; key: string; label: string; description: string | null;
  required: boolean; public: boolean; faceted: boolean; field_type: MetadataFieldType;
}
export interface MetadataSchema { schema_id: string; revision: number; fields: MetadataFieldDefinition[]; }
export type MetadataFieldValue =
  | { kind: "text"; value: string }
  | { kind: "number"; value: number }
  | { kind: "date_time_unix_ms"; value: number }
  | { kind: "boolean"; value: boolean }
  | { kind: "controlled_choice"; value: string };
export interface AssetMetadataValue { field_id: string; value: MetadataFieldValue; }
export interface AssetMetadataRecord {
  metadata_id: string; asset_id: string; schema_id: string; schema_revision: number;
  revision: number; values: AssetMetadataValue[];
}
export interface SchemaHistoryEntry { schema: MetadataSchema; changed_by: string; occurred_at_unix_ms: number; }
export interface RecordHistoryEntry { record: AssetMetadataRecord; changed_by: string; occurred_at_unix_ms: number; }

/** Build a schema mutation with only browser-owned facts. */
export function schemaWriteBody(fields: MetadataFieldDefinition[], expectedRevision: number | null) {
  return { fields, expected_revision: expectedRevision };
}

/** Build a record mutation with only browser-owned facts. Actor, time, and revisions are server-owned. */
export function recordWriteBody(assetId: string, schemaId: string, values: AssetMetadataValue[], expectedRevision: number | null) {
  return { asset_id: assetId, schema_id: schemaId, values, expected_revision: expectedRevision };
}

export function metadataHistoryLimit(limit = 12): number {
  return Math.max(1, Math.min(128, Math.trunc(limit)));
}

export function metadataFieldLabel(field: MetadataFieldDefinition): string {
  return field.label || field.key;
}

export function controlledChoiceValue(choiceId: string): MetadataFieldValue {
  return { kind: "controlled_choice", value: choiceId };
}

/** datetime-local is a wall-clock value; format and parse it in the local zone. */
export function datetimeLocalValue(unixMs: number): string {
  const date = new Date(unixMs);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function parseDatetimeLocal(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hours, minutes] = match;
  const date = new Date(Number(year), Number(month) - 1, Number(day), Number(hours), Number(minutes), 0, 0);
  return Number.isNaN(date.getTime()) ? null : date.getTime();
}
