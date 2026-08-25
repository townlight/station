import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiRequest } from "./api-client";
import {
  controlledChoiceValue, datetimeLocalValue, metadataFieldLabel, metadataHistoryLimit, parseDatetimeLocal, recordWriteBody, schemaWriteBody,
  type AssetMetadataRecord, type AssetMetadataValue, type ControlledChoice,
  type MetadataFieldDefinition, type MetadataFieldType, type MetadataFieldValue,
  type MetadataSchema, type RecordHistoryEntry, type SchemaHistoryEntry,
} from "./metadata-contract";

interface MetadataAsset { asset_id: string; media_path: string; }
interface OperatorSession { session: { roles: string[] } }

const newId = () => crypto.randomUUID();
const emptyField = (): MetadataFieldDefinition => ({
  field_id: newId(), key: "new_field", label: "New field", description: null,
  required: false, public: false, faceted: false,
  field_type: { kind: "text", constraints: { min_chars: 0, max_chars: 2048 } },
});
const fieldKind = (field: MetadataFieldDefinition): MetadataFieldType["kind"] => field.field_type.kind;
const fieldValue = (draft: AssetMetadataValue[], record: AssetMetadataRecord | null, field: MetadataFieldDefinition): MetadataFieldValue => {
  const existing = draft.find((value) => value.field_id === field.field_id)?.value ?? record?.values.find((value) => value.field_id === field.field_id)?.value;
  if (existing) return existing;
  switch (fieldKind(field)) {
    case "boolean": return { kind: "boolean", value: false };
    case "number": return { kind: "number", value: 0 };
    case "date_time": return { kind: "date_time_unix_ms", value: 0 };
    case "controlled_choice": return { kind: "controlled_choice", value: "" };
    default: return { kind: "text", value: "" };
  }
};

export function MetadataPanel({ assets }: { assets: MetadataAsset[] }) {
  const [schemas, setSchemas] = useState<MetadataSchema[]>([]);
  const [schema, setSchema] = useState<MetadataSchema | null>(null);
  const [schemaDraft, setSchemaDraft] = useState<MetadataFieldDefinition[]>([]);
  const [schemaHistory, setSchemaHistory] = useState<SchemaHistoryEntry[]>([]);
  const [selectedAssetId, setSelectedAssetId] = useState(assets[0]?.asset_id ?? "");
  const [records, setRecords] = useState<AssetMetadataRecord[]>([]);
  const [record, setRecord] = useState<AssetMetadataRecord | null>(null);
  const [recordDraft, setRecordDraft] = useState<AssetMetadataValue[]>([]);
  const [recordHistory, setRecordHistory] = useState<RecordHistoryEntry[]>([]);
  const [setupAdmin, setSetupAdmin] = useState(false);
  const [operator, setOperator] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [schemaConflict, setSchemaConflict] = useState(false);
  const [recordConflict, setRecordConflict] = useState(false);

  useEffect(() => { if (!selectedAssetId && assets[0]) setSelectedAssetId(assets[0].asset_id); }, [assets, selectedAssetId]);

  const load = useCallback(async () => {
    setBusy("load"); setError(null);
    try {
      const [nextSchemas, session] = await Promise.all([
        apiRequest<MetadataSchema[]>("/api/v1/custom-metadata/schemas"),
        apiRequest<OperatorSession>("/api/v1/auth/session"),
      ]);
      setSchemas(nextSchemas);
      setSetupAdmin(session.session.roles.includes("setup_admin"));
      setOperator(session.session.roles.some((role) => role === "setup_admin" || role === "meeting_operator"));
      setSchema((current) => nextSchemas.find((item) => item.schema_id === current?.schema_id) ?? nextSchemas[0] ?? null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Metadata authority could not be read."); }
    finally { setBusy(null); }
  }, []);

  const loadSchemaHistory = useCallback(async (schemaId: string) => {
    try { setSchemaHistory(await apiRequest<SchemaHistoryEntry[]>(`/api/v1/custom-metadata/schemas/${encodeURIComponent(schemaId)}/history?limit=${metadataHistoryLimit()}`)); }
    catch (problem) { setError(problem instanceof Error ? problem.message : "Schema history could not be read."); }
  }, []);

  async function readSchemaAuthority() {
    if (!schema) return;
    setBusy("schema-read"); setError(null);
    try {
      const [fresh, history] = await Promise.all([
        apiRequest<MetadataSchema>(`/api/v1/custom-metadata/schemas/${encodeURIComponent(schema.schema_id)}`),
        apiRequest<SchemaHistoryEntry[]>(`/api/v1/custom-metadata/schemas/${encodeURIComponent(schema.schema_id)}/history?limit=${metadataHistoryLimit()}`),
      ]);
      setSchema(fresh); setSchemaDraft(fresh.fields); setSchemaHistory(history); setSchemaConflict(false); setNotice(`Read authoritative schema revision ${fresh.revision}.`);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "The metadata schema could not be read."); }
    finally { setBusy(null); }
  }

  const loadRecords = useCallback(async (assetId: string) => {
    if (!assetId) { setRecords([]); setRecord(null); return; }
    try {
      const next = await apiRequest<AssetMetadataRecord[]>(`/api/v1/custom-metadata/assets/${encodeURIComponent(assetId)}/records`);
      setRecords(next);
      setRecord((current) => next.find((item) => item.metadata_id === current?.metadata_id) ?? next[0] ?? null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Asset metadata records could not be read."); }
  }, []);

  const loadRecordHistory = useCallback(async (metadataId: string) => {
    try { setRecordHistory(await apiRequest<RecordHistoryEntry[]>(`/api/v1/custom-metadata/records/${encodeURIComponent(metadataId)}/history?limit=${metadataHistoryLimit()}`)); }
    catch (problem) { setError(problem instanceof Error ? problem.message : "Metadata history could not be read."); }
  }, []);

  async function readRecordAuthority() {
    if (!record) return;
    setBusy("record-read"); setError(null);
    try {
      const [fresh, history] = await Promise.all([
        apiRequest<AssetMetadataRecord>(`/api/v1/custom-metadata/records/${encodeURIComponent(record.metadata_id)}`),
        apiRequest<RecordHistoryEntry[]>(`/api/v1/custom-metadata/records/${encodeURIComponent(record.metadata_id)}/history?limit=${metadataHistoryLimit()}`),
      ]);
      setRecord(fresh); setRecordDraft(fresh.values); setRecordHistory(history); setRecordConflict(false); setNotice(`Read authoritative metadata revision ${fresh.revision}.`);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "The metadata record could not be read."); }
    finally { setBusy(null); }
  }

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (schema) { setSchemaDraft(schema.fields); void loadSchemaHistory(schema.schema_id); } else setSchemaDraft([emptyField()]); }, [loadSchemaHistory, schema]);
  useEffect(() => { void loadRecords(selectedAssetId); }, [loadRecords, selectedAssetId]);
  useEffect(() => { setRecordDraft(record?.values ?? []); if (record) void loadRecordHistory(record.metadata_id); else setRecordHistory([]); }, [loadRecordHistory, record]);

  const selectedAsset = assets.find((asset) => asset.asset_id === selectedAssetId);
  const schemaForRecord = schema;
  const recordForSchema = useMemo(() => records.find((item) => item.schema_id === schemaForRecord?.schema_id) ?? null, [records, schemaForRecord]);
  useEffect(() => { setRecord(recordForSchema); setRecordDraft(recordForSchema?.values ?? []); setRecordConflict(false); }, [recordForSchema]);

  function updateField(index: number, update: Partial<MetadataFieldDefinition>) {
    setSchemaDraft((fields) => fields.map((field, position) => position === index ? { ...field, ...update } : field));
  }
  function updateType(index: number, kind: MetadataFieldType["kind"]) {
    const defaults: Record<MetadataFieldType["kind"], MetadataFieldType> = {
      text: { kind: "text", constraints: { min_chars: 0, max_chars: 2048 } },
      number: { kind: "number", constraints: { minimum: -2147483648, maximum: 2147483647 } },
      date_time: { kind: "date_time", constraints: { minimum_unix_ms: null, maximum_unix_ms: null } },
      boolean: { kind: "boolean" },
      controlled_choice: { kind: "controlled_choice", constraints: { choices: [{ choice_id: newId(), key: "choice", label: "Choice" }] } },
    };
    updateField(index, { field_type: defaults[kind] });
  }

  async function saveSchema(event: FormEvent) {
    event.preventDefault(); if (!setupAdmin || !schemaDraft.length) return;
    setBusy("schema"); setError(null); setNotice(null); setSchemaConflict(false);
    const schemaId = schema?.schema_id ?? newId();
    try {
      const saved = await apiRequest<MetadataSchema>(`/api/v1/custom-metadata/schemas/${encodeURIComponent(schemaId)}`, {
        method: "PUT", body: JSON.stringify(schemaWriteBody(schemaDraft, schema?.revision ?? null)),
      });
      setSchema(saved); setNotice(`Schema revision ${saved.revision} saved.`); await load();
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) { setSchemaConflict(true); setNotice(problem.code === "metadata_identity_rebound" ? "This schema identity cannot be rebound. Read the current schema before making another revision." : "This schema revision is stale. Read the current authority before saving again."); }
      setError(problem instanceof Error ? problem.message : "Schema could not be saved.");
    } finally { setBusy(null); }
  }

  function updateRecordValue(field: MetadataFieldDefinition, value: MetadataFieldValue) {
    setRecordDraft((values) => [...values.filter((item) => item.field_id !== field.field_id), { field_id: field.field_id, value }]);
  }
  async function saveRecord(event: FormEvent) {
    event.preventDefault(); if (!operator || !selectedAssetId || !schemaForRecord) return;
    setBusy("record"); setError(null); setNotice(null); setRecordConflict(false);
    const metadataId = record?.metadata_id ?? newId();
    try {
      const saved = await apiRequest<AssetMetadataRecord>(`/api/v1/custom-metadata/records/${encodeURIComponent(metadataId)}`, {
        method: "PUT", body: JSON.stringify(recordWriteBody(selectedAssetId, schemaForRecord.schema_id, recordDraft, record?.revision ?? null)),
      });
      setRecord(saved); setNotice(`Metadata revision ${saved.revision} saved.`); await loadRecords(selectedAssetId);
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) { setRecordConflict(true); setNotice(problem.code === "metadata_identity_rebound" ? "This metadata record is bound to its original asset and schema. Read the current record before retrying." : "This metadata record revision is stale. Read the current authority before saving again."); await loadRecords(selectedAssetId); }
      setError(problem instanceof Error ? problem.message : "Metadata record could not be saved.");
    } finally { setBusy(null); }
  }

  return <>
    <header className="page-header"><div><p className="eyebrow">Asset-bound authority</p><h1>Metadata</h1><p>Define reusable metadata schemas and maintain auditable values for each media asset.</p></div><div className="header-actions"><button className="secondary" onClick={() => void load()} disabled={busy !== null}>Refresh authority</button></div></header>
    {error && <div className="error-box" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    {!operator && <div className="metadata-boundary" role="status"><strong>Metadata access is restricted</strong><span>Setup Admin or Meeting Operator is required to read and maintain asset metadata.</span></div>}
    <section className="metadata-layout">
      <article className="panel metadata-schemas"><div className="panel-head"><div><p className="eyebrow">Definitions</p><h2>Metadata schemas</h2></div><span className="count">{schemas.length}</span></div>
        {!schemas.length ? <div className="metadata-empty"><strong>No schemas yet</strong><p>Setup Admin can create the first schema for this station.</p></div> : <div className="metadata-schema-list">{schemas.map((item) => <button key={item.schema_id} className={schema?.schema_id === item.schema_id ? "selected" : ""} onClick={() => setSchema(item)}><span><strong>{item.fields.map(metadataFieldLabel).slice(0, 2).join(" · ") || "Untitled schema"}</strong><small><code>{item.schema_id.slice(0, 8)}…</code> · revision {item.revision}</small></span><b>{item.fields.length}</b></button>)}</div>}
        {setupAdmin && <button className="secondary wide" onClick={() => { setSchema(null); setSchemaDraft([emptyField()]); setSchemaConflict(false); }}>Create schema</button>}
        {schema && <details className="metadata-history"><summary>Schema history ({schemaHistory.length})</summary>{schemaHistory.length ? schemaHistory.map((entry) => <div key={`${entry.schema.revision}-${entry.occurred_at_unix_ms}`}><strong>Revision {entry.schema.revision}</strong><small>{entry.changed_by} · {formatMetadataTime(entry.occurred_at_unix_ms)}</small></div>) : <p className="muted">No history available.</p>}</details>}
      </article>
      <article className="panel metadata-editor"><div className="panel-head"><div><p className="eyebrow">Schema authority</p><h2>{schema ? `Revision ${schema.revision}` : "Create schema"}</h2></div>{schema && <div className="button-row"><button type="button" className="text-button" onClick={() => void readSchemaAuthority()} disabled={busy !== null}>Read current</button><span className="pill pill-good">immutable identity</span></div>}</div>
        {!setupAdmin && <div className="metadata-boundary"><strong>Setup Admin only</strong><span>Meeting Operators can edit asset values but cannot change schema definitions.</span></div>}
        <form onSubmit={saveSchema}><div className="metadata-fields">{schemaDraft.map((field, index) => <MetadataFieldEditor key={field.field_id} field={field} index={index} disabled={!setupAdmin || busy !== null} onUpdate={updateField} onType={updateType} />)}</div>{schemaConflict && <div className="metadata-conflict" role="alert"><strong>Authoritative schema reload required</strong><span>Read the current schema revision before writing again.</span></div>}<div className="button-row"><button type="button" className="secondary" disabled={!setupAdmin || busy !== null} onClick={() => setSchemaDraft((fields) => [...fields, emptyField()])}>Add field</button><button className="primary" disabled={!setupAdmin || busy !== null || schemaConflict || !schemaDraft.length}>{busy === "schema" ? "Saving…" : schema ? "Save schema revision" : "Create schema"}</button></div></form>
      </article>
    </section>
    <section className="panel metadata-records"><div className="panel-head"><div><p className="eyebrow">Per-asset values</p><h2>Asset metadata records</h2></div><span className="count">{records.length} on selected asset</span></div>
      <label>Media asset<select value={selectedAssetId} onChange={(event) => setSelectedAssetId(event.target.value)}><option value="">Select an asset</option>{assets.map((asset) => <option key={asset.asset_id} value={asset.asset_id}>{asset.media_path.split(/[\\/]/).pop()} · {asset.asset_id.slice(0, 10)}…</option>)}</select></label>
        {!selectedAsset ? <div className="metadata-empty"><strong>Select a media asset</strong><p>Import a media asset before recording metadata values.</p></div> : !schemaForRecord ? <div className="metadata-empty"><strong>Select or create a schema</strong><p>Choose a schema above before editing this asset.</p></div> : <form onSubmit={saveRecord}><div className="metadata-record-head"><div><strong>{selectedAsset.media_path.split(/[\\/]/).pop()}</strong><small>{record ? `Record revision ${record.revision} · schema revision ${record.schema_revision}` : "New record · server assigns its revision"}</small></div>{record && <button type="button" className="secondary" onClick={() => void readRecordAuthority()} disabled={busy !== null}>Read current</button>}</div><div className="metadata-value-grid">{schemaForRecord.fields.map((field) => <MetadataValueEditor key={field.field_id} field={field} value={fieldValue(recordDraft, record, field)} disabled={!operator || busy !== null} onChange={(value) => updateRecordValue(field, value)} />)}</div>{recordConflict && <div className="metadata-conflict" role="alert"><strong>Authoritative record reload required</strong><span>Read the current metadata revision before writing again.</span></div>}<div className="button-row"><button className="primary" disabled={!operator || busy !== null || recordConflict}>{busy === "record" ? "Saving…" : record ? "Save value revision" : "Create value record"}</button></div></form>}
      {record && <details className="metadata-history" open><summary>Record history ({recordHistory.length})</summary>{recordHistory.length ? recordHistory.map((entry) => <div key={`${entry.record.revision}-${entry.occurred_at_unix_ms}`}><strong>Revision {entry.record.revision}</strong><small>{entry.changed_by} · {formatMetadataTime(entry.occurred_at_unix_ms)}</small></div>) : <p className="muted">No history available.</p>}</details>}
    </section>
  </>;
}

function MetadataFieldEditor({ field, index, disabled, onUpdate, onType }: { field: MetadataFieldDefinition; index: number; disabled: boolean; onUpdate: (index: number, update: Partial<MetadataFieldDefinition>) => void; onType: (index: number, kind: MetadataFieldType["kind"]) => void }) {
  return <fieldset className="metadata-field-editor"><legend>Field {index + 1}</legend><div className="metadata-field-grid"><label>Label<input required maxLength={120} value={field.label} disabled={disabled} onChange={(event) => onUpdate(index, { label: event.target.value })} /></label><label>Key<input required maxLength={64} pattern="[a-z0-9_]+" value={field.key} disabled={disabled} onChange={(event) => onUpdate(index, { key: event.target.value })} /><small>Stable lowercase key; existing keys cannot be rebound.</small></label><label>Type<select value={fieldKind(field)} disabled={disabled} onChange={(event) => onType(index, event.target.value as MetadataFieldType["kind"])}><option value="text">Text</option><option value="number">Number</option><option value="date_time">Date/time</option><option value="boolean">Boolean</option><option value="controlled_choice">Controlled choice</option></select></label><label>Description<textarea value={field.description ?? ""} disabled={disabled} onChange={(event) => onUpdate(index, { description: event.target.value || null })} /></label></div><div className="metadata-checks"><label><input type="checkbox" checked={field.required} disabled={disabled} onChange={(event) => onUpdate(index, { required: event.target.checked })} /> Required</label><label><input type="checkbox" checked={field.public} disabled={disabled} onChange={(event) => onUpdate(index, { public: event.target.checked })} /> Public projection</label><label><input type="checkbox" checked={field.faceted} disabled={disabled} onChange={(event) => onUpdate(index, { faceted: event.target.checked })} /> Search facet</label></div></fieldset>;
}

function MetadataValueEditor({ field, value, disabled, onChange }: { field: MetadataFieldDefinition; value: MetadataFieldValue; disabled: boolean; onChange: (value: MetadataFieldValue) => void }) {
  const type = field.field_type;
  const label = <span>{metadataFieldLabel(field)}{field.required && <b>*</b>}</span>;
  if (type.kind === "boolean") return <label className="metadata-boolean"><span>{label}</span><input type="checkbox" checked={value.kind === "boolean" ? value.value : false} disabled={disabled} onChange={(event) => onChange({ kind: "boolean", value: event.target.checked })} /></label>;
  if (type.kind === "controlled_choice") return <label>{label}<select value={value.kind === "controlled_choice" ? value.value : ""} disabled={disabled} onChange={(event) => onChange(controlledChoiceValue(event.target.value))}><option value="">Not set</option>{type.constraints.choices.map((choice: ControlledChoice) => <option key={choice.choice_id} value={choice.choice_id}>{choice.label}</option>)}</select></label>;
  if (type.kind === "number") return <label>{label}<input type="number" min={type.constraints.minimum} max={type.constraints.maximum} value={value.kind === "number" ? value.value : ""} disabled={disabled} onChange={(event) => onChange({ kind: "number", value: event.target.value === "" ? 0 : Number(event.target.value) })} /></label>;
  if (type.kind === "date_time") return <label>{label}<input type="datetime-local" value={value.kind === "date_time_unix_ms" && value.value ? datetimeLocalValue(value.value) : ""} disabled={disabled} onChange={(event) => onChange({ kind: "date_time_unix_ms", value: parseDatetimeLocal(event.target.value) ?? 0 })} /></label>;
  return <label>{label}<input maxLength={type.constraints.max_chars} minLength={type.constraints.min_chars} value={value.kind === "text" ? value.value : ""} disabled={disabled} onChange={(event) => onChange({ kind: "text", value: event.target.value })} /></label>;
}

function formatMetadataTime(unixMs: number): string { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(unixMs); }
