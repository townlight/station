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
