export type PublicFacetValue =
  | { kind: "text"; value: string }
  | { kind: "number"; value: number }
  | { kind: "date_time_unix_ms"; value: number }
  | { kind: "boolean"; value: boolean }
  | { kind: "controlled_choice"; value: string };
export interface PublicCatalogItem { asset_id: string; title: string; published_at_utc_ms: number; duration_ms: number; facets: { field_key: string; value: PublicFacetValue }[] }
export interface PublicCatalogPage { items: PublicCatalogItem[]; next_cursor: string | null }

const exact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length && keys.every((key) => key in value);
const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const safeInteger = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value);
function parseValue(value: unknown): PublicFacetValue {
  if (!object(value) || !exact(value, ["kind", "value"]) || typeof value.kind !== "string") throw new Error("Public catalog facet value is malformed.");
  if ((value.kind === "text" || value.kind === "controlled_choice") && typeof value.value === "string") return value as PublicFacetValue;
  if ((value.kind === "number" || value.kind === "date_time_unix_ms") && safeInteger(value.value)) return value as PublicFacetValue;
  if (value.kind === "boolean" && typeof value.value === "boolean") return value as PublicFacetValue;
  throw new Error("Public catalog facet type is invalid.");
}
export function parsePublicCatalogPage(value: unknown): PublicCatalogPage {
  if (!object(value) || !exact(value, ["items", "next_cursor"]) || !Array.isArray(value.items) || value.items.length > 100 || !(value.next_cursor === null || typeof value.next_cursor === "string" && value.next_cursor.length <= 240)) throw new Error("Public catalog response is malformed or unbounded.");
  const ids = new Set<string>();
  const items = value.items.map((candidate) => {
    if (!object(candidate) || !exact(candidate, ["asset_id", "title", "published_at_utc_ms", "duration_ms", "facets"]) || typeof candidate.asset_id !== "string" || !/^[0-9a-f]{64}$/.test(candidate.asset_id) || ids.has(candidate.asset_id) || typeof candidate.title !== "string" || new TextEncoder().encode(candidate.title).length > 240 || !safeInteger(candidate.published_at_utc_ms) || candidate.published_at_utc_ms < 0 || !safeInteger(candidate.duration_ms) || candidate.duration_ms <= 0 || !Array.isArray(candidate.facets) || candidate.facets.length > 128) throw new Error("Public catalog item is malformed or unbounded.");
    ids.add(candidate.asset_id);
    const keys = new Set<string>();
    const facets = candidate.facets.map((facet) => { if (!object(facet) || !exact(facet, ["field_key", "value"]) || typeof facet.field_key !== "string" || !/^[a-z0-9_-]{1,64}$/.test(facet.field_key) || keys.has(facet.field_key)) throw new Error("Public catalog facets are malformed."); keys.add(facet.field_key); return { field_key: facet.field_key, value: parseValue(facet.value) }; });
    return { asset_id: candidate.asset_id, title: candidate.title, published_at_utc_ms: candidate.published_at_utc_ms, duration_ms: candidate.duration_ms, facets };
  });
  return { items, next_cursor: value.next_cursor as string | null };
}
