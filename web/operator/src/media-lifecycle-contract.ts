export type MediaLifecycleAuthority = {
  asset_id: string;
  retention: { revision: number; permanent: boolean; review_after_unix_ms: number | null; legal_hold: boolean; disposition: "retained" | "review_required" | "legal_hold" } | null;
  replacement: { successor_asset_id: string } | null;
  thumbnail: { thumbnail_sha256: string } | null;
  recovery_history: Array<{ revision: number; recovered_asset_sha256: string; byte_count: number; changed_by: string; occurred_at_unix_ms: number }>;
};

export type MediaLifecycleViewState = {
  state_asset_id: string;
  authority: MediaLifecycleAuthority | null;
  message: string | null;
  busy: boolean;
};

export function mediaLifecycleRenderState(assetId: string, state: MediaLifecycleViewState): MediaLifecycleViewState {
  if (state.state_asset_id === assetId && (!state.authority || state.authority.asset_id === assetId)) return state;
  return { state_asset_id: assetId, authority: null, message: null, busy: false };
}

export type AssetOperationOwner = { asset_id: string; generation: number };

export function assetOperationStillOwns(
  owner: AssetOperationOwner,
  currentAssetId: string,
  currentGeneration: number,
): boolean {
  return owner.asset_id === currentAssetId && owner.generation === currentGeneration;
}

export type MediaLifecycleDraft = {
  draft_asset_id: string;
  source_path: string;
  review_after: string;
  permanent: boolean;
  legal_hold: boolean;
};

export function mediaLifecycleDraftForAsset(assetId: string, draft: MediaLifecycleDraft): MediaLifecycleDraft {
  if (draft.draft_asset_id === assetId) return draft;
  return { draft_asset_id: assetId, source_path: "", review_after: "", permanent: false, legal_hold: false };
}

export function mediaLifecycleDraftCanSubmit(
  assetId: string,
  draft: MediaLifecycleDraft,
  authority: MediaLifecycleAuthority | null,
): boolean {
  return draft.draft_asset_id === assetId && authority?.asset_id === assetId;
}
