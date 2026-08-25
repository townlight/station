use std::collections::BTreeMap;

use sha2::{Digest, Sha256};
use station_catalog::CatalogAssetRecord;
use station_metadata::{AssetMetadataRecord, MetadataProjectionValue, project_search_facets};
use station_schedule_automation::{
    AssetCandidate, MAX_CANDIDATE_PROJECTION_ASSETS, MetadataValue,
    canonical_candidate_projection_sha256,
};

use super::*;

/// An unauthenticated, one-transaction S19 candidate-source snapshot.
///
/// This boundary deliberately does not issue a receipt. A later trusted
/// issuer must authenticate `candidate_projection_sha256` for an exact S19
/// materialization scope before the pure materializer can use this snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogCandidateProjection {
    pub candidates: Vec<AssetCandidate>,
    pub candidate_projection_sha256: String,
}

impl StationStore {
    /// Builds the complete, bounded candidate union from one SQLite read
    /// snapshot. Only explicitly published catalog records with a current
    /// ready media row are represented. Paths, filenames, and ingest state
    /// are never consulted or inferred.
    pub fn snapshot_catalog_candidates(&self) -> Result<CatalogCandidateProjection, String> {
        self.execute("BEGIN")?;
        let outcome = (|| {
            let catalog_records = self.read_catalog_projection_records()?;
            let mut candidates = Vec::new();
            for catalog in catalog_records {
                self.validate_current_catalog_snapshot(&catalog)?;
                if !catalog.is_published() {
                    continue;
                }
                let Some(media) = self.read_media_asset(&catalog.asset_sha256)? else {
                    continue;
                };
                media.validate().map_err(|error| {
                    format!("database returned invalid catalog media authority: {error:?}")
                })?;
                if media.readiness != AssetReadiness::Ready {
                    continue;
                }

                let (indexed_custom_metadata, custom_metadata_authority_sha256) =
                    self.project_catalog_metadata(&catalog, &media)?;
                let published_at_utc_ms = catalog.published_at_utc_ms.ok_or_else(|| {
                    "published catalog record has no publication time".to_string()
                })?;
                candidates.push(AssetCandidate {
                    asset_sha256: catalog.asset_sha256,
                    title: catalog.title,
                    meeting_body: catalog.meeting_body,
                    duration_ms: media.duration_ms,
                    ready: true,
                    published: true,
                    published_at_utc_ms,
                    custom_metadata_authority_sha256,
                    indexed_custom_metadata,
                });
                if candidates.len() > MAX_CANDIDATE_PROJECTION_ASSETS {
                    return Err(
                        "catalog candidate projection exceeds its bounded union capacity".into(),
                    );
                }
            }
            candidates.sort_by(|left, right| left.asset_sha256.cmp(&right.asset_sha256));
            let candidate_projection_sha256 = canonical_candidate_projection_sha256(&candidates)?;
            Ok(CatalogCandidateProjection {
                candidates,
                candidate_projection_sha256,
            })
        })();
        self.finish_value_transaction(outcome)
    }

    fn read_catalog_projection_records(&self) -> Result<Vec<CatalogAssetRecord>, String> {
        let statement = self.prepare(
            "SELECT asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json \
             FROM catalog_assets ORDER BY asset_sha256 LIMIT ?1",
        )?;
        bind_i64(
            self.database,
            statement.raw,
            1,
            (catalog_authority::MAX_CATALOG_ASSETS + 1) as i64,
        )?;
        let mut records = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    records.push(catalog_authority::read_catalog_asset_row(statement.raw)?)
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        if records.len() > catalog_authority::MAX_CATALOG_ASSETS {
            return Err("catalog authority exceeds its bounded asset capacity".into());
        }
        Ok(records)
    }

    fn project_catalog_metadata(
        &self,
        catalog: &CatalogAssetRecord,
        media: &MediaAsset,
    ) -> Result<(BTreeMap<String, MetadataValue>, String), String> {
        let records = self.list_asset_metadata_records(&catalog.asset_sha256)?;
        let mut facets = BTreeMap::new();
        let mut digest = Sha256::new();
        digest_put(&mut digest, "townlight.catalog-candidate-provenance.v1");
        digest_put(&mut digest, &catalog.asset_sha256);
        digest.update(catalog.revision.to_le_bytes());
        digest_put(&mut digest, &catalog.title);
        match &catalog.meeting_body {
            Some(value) => {
                digest.update([1]);
                digest_put(&mut digest, value);
            }
            None => digest.update([0]),
        }
        digest.update(catalog.published_at_utc_ms.unwrap_or(-1).to_le_bytes());
        // Media storage has no revision field. Bind only the executable media
        // facts this adapter uses, never its path or ingest metadata.
        digest_put(&mut digest, &media.asset_id);
        digest.update(media.duration_ms.to_le_bytes());
        digest.update([u8::from(media.readiness == AssetReadiness::Ready)]);

        digest.update((records.len() as u32).to_le_bytes());
        for record in records {
            let historical = self.read_exact_asset_metadata_history(&record)?;
            if historical != record {
                return Err(
                    "current asset metadata disagrees with its exact historical snapshot".into(),
                );
            }
            let schema = self
                .read_custom_metadata_schema_revision(&record.schema_id, record.schema_revision)?
                .ok_or_else(|| {
                    "asset metadata references unavailable historical schema".to_string()
                })?;
            self.validate_stored_asset_metadata_record(&record)?;
            let projected = project_search_facets(&record, &schema)?;
            digest_put(&mut digest, &record.metadata_id);
            digest_put(&mut digest, &record.schema_id);
            digest.update(record.schema_revision.to_le_bytes());
            digest.update(record.revision.to_le_bytes());
            digest.update((projected.len() as u32).to_le_bytes());
            for facet in projected {
                let value = metadata_value_from_projection(&facet.value);
                if facets
                    .insert(facet.field_key.clone(), value.clone())
                    .is_some()
                {
                    return Err(
                        "catalog candidate projection has duplicate metadata facet keys".into(),
                    );
                }
                digest_put(&mut digest, &facet.field_key);
                digest_metadata_value(&mut digest, &value);
            }
        }
        Ok((facets, format!("{:x}", digest.finalize())))
    }

    fn read_exact_asset_metadata_history(
        &self,
        current: &AssetMetadataRecord,
    ) -> Result<AssetMetadataRecord, String> {
        let statement = self.prepare(
            "SELECT metadata_id,asset_id,schema_id,schema_revision,revision,record_json \
             FROM asset_metadata_record_history WHERE metadata_id=?1 AND revision=?2",
        )?;
        let metadata_id = c_string(&current.metadata_id, "asset metadata identity")?;
        bind_text(self.database, statement.raw, 1, &metadata_id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(current.revision, "asset metadata revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let historical = read_asset_metadata_record_row(statement.raw, 0, 1, 2, 3, 4, 5)?;
                self.validate_stored_asset_metadata_record(&historical)?;
                Ok(historical)
            }
            SQLITE_DONE => Err("current asset metadata has no exact historical snapshot".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }
}

fn metadata_value_from_projection(value: &MetadataProjectionValue) -> MetadataValue {
    match value {
        MetadataProjectionValue::Text(value) => MetadataValue::Text(value.clone()),
        MetadataProjectionValue::Number(value) => MetadataValue::Number(*value),
        MetadataProjectionValue::DateTimeUnixMs(value) => MetadataValue::DateTimeUnixMs(*value),
        MetadataProjectionValue::Boolean(value) => MetadataValue::Boolean(*value),
        MetadataProjectionValue::ControlledChoice(value) => MetadataValue::Choice(value.clone()),
    }
}

fn digest_put(digest: &mut Sha256, value: &str) {
    digest.update((value.len() as u64).to_le_bytes());
    digest.update(value.as_bytes());
}

fn digest_metadata_value(digest: &mut Sha256, value: &MetadataValue) {
    match value {
        MetadataValue::Text(value) => {
            digest.update([0]);
            digest_put(digest, value);
        }
        MetadataValue::Number(value) => {
            digest.update([1]);
            digest.update(value.to_le_bytes());
        }
        MetadataValue::Boolean(value) => digest.update([2, u8::from(*value)]),
        MetadataValue::DateTimeUnixMs(value) => {
            digest.update([3]);
            digest.update(value.to_le_bytes());
        }
        MetadataValue::Choice(value) => {
            digest.update([4]);
            digest_put(digest, value);
        }
    }
}
