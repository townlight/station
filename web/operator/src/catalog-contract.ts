export type CatalogEditorialState = "draft" | "published" | "withdrawn";

export interface CatalogAssetRecord {
  asset_sha256: string;
  title: string;
  meeting_body: string | null;
  editorial_state: CatalogEditorialState;
  published_at_utc_ms: number | null;
  revision: number;
}

export interface CatalogAssetHistoryEntry {
  record: CatalogAssetRecord;
  changed_by: string;
  occurred_at_utc_ms: number;
}

export interface CatalogAssetPage {
  records: CatalogAssetRecord[];
  next_after_asset_sha256: string | null;
}

export interface CatalogAssetWriteBody {
  title: string;
  meeting_body: string | null;
  expected_revision: number | null;
}

export interface CatalogAssetTransitionBody {
  expected_revision: number;
}

export type CatalogAssetTransition = "publish" | "withdraw";

const RECORD_KEYS = ["asset_sha256", "title", "meeting_body", "editorial_state", "published_at_utc_ms", "revision"];
const HISTORY_KEYS = ["record", "changed_by", "occurred_at_utc_ms"];
const PAGE_KEYS = ["records", "next_after_asset_sha256"];
const STATES = new Set<CatalogEditorialState>(["draft", "published", "withdrawn"]);
const CATALOG_PAGE_LIMIT = 1_000;
const MAX_CATALOG_RECORDS = 100_000;
export const MAX_CATALOG_TEXT_BYTES = 240;

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value).sort();
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index]);
}

function object(value: unknown, description: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${description} is malformed.`);
  return value as Record<string, unknown>;
}

function nonnegativeInteger(value: unknown, description: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error(`${description} is malformed.`);
  return value;
}

export function parseCatalogAssetRecord(value: unknown): CatalogAssetRecord {
  const source = object(value, "Catalog asset");
  if (!exactKeys(source, RECORD_KEYS)
    || typeof source.asset_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(source.asset_sha256)
    || typeof source.title !== "string"
    || (source.meeting_body !== null && typeof source.meeting_body !== "string")
    || typeof source.editorial_state !== "string"
    || !STATES.has(source.editorial_state as CatalogEditorialState)
    || (source.published_at_utc_ms !== null && (typeof source.published_at_utc_ms !== "number" || !Number.isSafeInteger(source.published_at_utc_ms) || source.published_at_utc_ms < 0))) {
    throw new Error("Catalog asset is malformed.");
  }
  const revision = nonnegativeInteger(source.revision, "Catalog asset revision");
  if (revision < 1) throw new Error("Catalog asset revision is malformed.");
  return {
    asset_sha256: source.asset_sha256,
    title: source.title,
    meeting_body: source.meeting_body,
    editorial_state: source.editorial_state as CatalogEditorialState,
    published_at_utc_ms: source.published_at_utc_ms as number | null,
    revision,
  };
}

export function parseCatalogAssetPage(value: unknown): CatalogAssetPage {
  const source = object(value, "Catalog asset page");
  if (!exactKeys(source, PAGE_KEYS) || !Array.isArray(source.records)
    || (source.next_after_asset_sha256 !== null
      && (typeof source.next_after_asset_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(source.next_after_asset_sha256)))) {
    throw new Error("Catalog asset page is malformed.");
  }
  return {
    records: source.records.map(parseCatalogAssetRecord),
    next_after_asset_sha256: source.next_after_asset_sha256 as string | null,
  };
}

export function parseCatalogAssetHistory(value: unknown): CatalogAssetHistoryEntry[] {
  if (!Array.isArray(value)) throw new Error("Catalog asset history is malformed.");
  return value.map((item) => {
    const source = object(item, "Catalog asset history entry");
    if (!exactKeys(source, HISTORY_KEYS) || typeof source.changed_by !== "string") throw new Error("Catalog asset history entry is malformed.");
    return {
      record: parseCatalogAssetRecord(source.record),
      changed_by: source.changed_by,
      occurred_at_utc_ms: nonnegativeInteger(source.occurred_at_utc_ms, "Catalog history occurrence time"),
    };
  });
}

export async function readAllCatalogAssetPages(request: (path: string) => Promise<unknown>): Promise<CatalogAssetRecord[]> {
  const records: CatalogAssetRecord[] = [];
  const seenAssets = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const query = cursor === null
      ? `?limit=${CATALOG_PAGE_LIMIT}`
      : `?after_asset_sha256=${encodeURIComponent(cursor)}&limit=${CATALOG_PAGE_LIMIT}`;
    const page = parseCatalogAssetPage(await request(`/api/v1/catalog/assets${query}`));
    for (const item of page.records) {
      if (seenAssets.has(item.asset_sha256) || records.length >= MAX_CATALOG_RECORDS) {
        throw new Error("Catalog pagination is duplicated or exceeds station capacity.");
      }
      seenAssets.add(item.asset_sha256);
      records.push(item);
    }
    cursor = page.next_after_asset_sha256;
    if (cursor !== null && (seenCursors.has(cursor) || records.length >= MAX_CATALOG_RECORDS)) {
      throw new Error("Catalog pagination cursor is cyclic or exceeds station capacity.");
    }
    if (cursor !== null) seenCursors.add(cursor);
  } while (cursor !== null);
  return records;
}

/** Draft create/edit payload. Lifecycle state is controlled by explicit transition routes. */
export function catalogWriteBody(record: Pick<CatalogAssetRecord, "title" | "meeting_body">, expectedRevision: number | null): CatalogAssetWriteBody {
  return {
    title: record.title,
    meeting_body: record.meeting_body,
    expected_revision: expectedRevision,
  };
}

export function catalogTransitionBody(expectedRevision: number): CatalogAssetTransitionBody {
  return { expected_revision: expectedRevision };
}

export function catalogTransitionPath(assetPath: string, transition: CatalogAssetTransition): string {
  return `${assetPath}/${transition}`;
}

export function catalogHistoryLimit(limit = 12): number {
  return Math.max(1, Math.min(128, Math.trunc(limit)));
}

export function catalogTextFits(value: string): boolean {
  return new TextEncoder().encode(value).byteLength <= MAX_CATALOG_TEXT_BYTES;
}

export function formatCatalogTime(unixMs: number): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(unixMs);
}
