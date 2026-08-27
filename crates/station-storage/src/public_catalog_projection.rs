use super::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_metadata::{
    MetadataFacet, MetadataFieldType, MetadataProjectionValue, project_public_facets,
};
use station_schedule::AssetReadiness;
use std::collections::HashSet;

pub const MAX_PUBLIC_CATALOG_PAGE_SIZE: usize = 100;
pub const MAX_PUBLIC_CATALOG_FILTERS: usize = 8;
pub const MAX_PUBLIC_CATALOG_FACETS: usize = 128;
const MAX_CANDIDATES: usize = 500;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicCatalogFilter {
    pub field_key: String,
    pub value: MetadataProjectionValue,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicCatalogItem {
    pub asset_id: String,
    pub title: String,
    pub published_at_utc_ms: i64,
    pub duration_ms: u64,
    pub facets: Vec<MetadataFacet>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublicCatalogCursor {
    pub version: u8,
    pub published_at_utc_ms: i64,
    pub asset_id: String,
    pub filter_digest: String,
    pub generation: u64,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicCatalogPage {
    pub items: Vec<PublicCatalogItem>,
    #[serde(skip)]
    pub next_cursor: Option<PublicCatalogCursor>,
}

impl StationStore {
    pub(super) fn validate_public_facet_schema_policy(
        &self,
        proposed: &MetadataSchema,
    ) -> Result<(), String> {
        let mut keys = HashSet::new();
        let mut count = 0usize;
        for schema in self
            .list_custom_metadata_schemas()?
            .into_iter()
            .filter(|schema| schema.schema_id != proposed.schema_id)
            .chain(std::iter::once(proposed.clone()))
        {
            for field in schema
                .fields
                .iter()
                .filter(|field| field.public && field.faceted)
            {
                count = count
                    .checked_add(1)
                    .ok_or("public facet capacity overflow")?;
                if !keys.insert(field.key.clone()) {
                    return Err("public facet keys must be globally unique across schemas".into());
                }
            }
        }
        if count > MAX_PUBLIC_CATALOG_FACETS {
            return Err("public facet authority cannot exceed 128 fields".into());
        }
        Ok(())
    }
    pub(super) fn initialize_public_catalog_projection_schema(&self) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let result = (|| {
            self.execute("CREATE TABLE IF NOT EXISTS public_catalog_identities(asset_sha256 TEXT PRIMARY KEY,public_id TEXT NOT NULL UNIQUE CHECK(length(public_id)=64));CREATE TABLE IF NOT EXISTS public_catalog_generation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation INTEGER NOT NULL CHECK(generation>=1));INSERT OR IGNORE INTO public_catalog_generation VALUES(1,1);
CREATE TRIGGER IF NOT EXISTS public_discovery_catalog_i AFTER INSERT ON catalog_assets BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_catalog_u AFTER UPDATE ON catalog_assets BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;
CREATE TRIGGER IF NOT EXISTS public_discovery_media_i AFTER INSERT ON media_assets BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_media_u AFTER UPDATE ON media_assets BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;
CREATE TRIGGER IF NOT EXISTS public_discovery_schema_i AFTER INSERT ON custom_metadata_schemas BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_schema_u AFTER UPDATE ON custom_metadata_schemas BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_schema_h AFTER INSERT ON custom_metadata_schema_history BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;
CREATE TRIGGER IF NOT EXISTS public_discovery_metadata_i AFTER INSERT ON asset_metadata_records BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_metadata_u AFTER UPDATE ON asset_metadata_records BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_metadata_h AFTER INSERT ON asset_metadata_record_history BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;CREATE TRIGGER IF NOT EXISTS public_discovery_catalog_h AFTER INSERT ON catalog_asset_history BEGIN UPDATE public_catalog_generation SET generation=generation+1 WHERE singleton=1;END;
CREATE TABLE IF NOT EXISTS public_catalog_generation_history(generation INTEGER PRIMARY KEY CHECK(generation BETWEEN 1 AND 1000001));INSERT OR IGNORE INTO public_catalog_generation_history SELECT generation FROM public_catalog_generation WHERE singleton=1;CREATE TRIGGER IF NOT EXISTS public_discovery_generation_capacity BEFORE UPDATE ON public_catalog_generation WHEN NEW.generation>1000001 BEGIN SELECT RAISE(ABORT,'public catalog generation capacity exceeded');END;CREATE TRIGGER IF NOT EXISTS public_discovery_generation_append AFTER UPDATE ON public_catalog_generation BEGIN INSERT INTO public_catalog_generation_history(generation) VALUES(NEW.generation);END;CREATE TRIGGER IF NOT EXISTS public_discovery_generation_history_no_update BEFORE UPDATE ON public_catalog_generation_history BEGIN SELECT RAISE(ABORT,'public catalog generation history is append-only');END;CREATE TRIGGER IF NOT EXISTS public_discovery_generation_history_no_delete BEFORE DELETE ON public_catalog_generation_history BEGIN SELECT RAISE(ABORT,'public catalog generation history is append-only');END;")?;
            let q =
                self.prepare("SELECT asset_sha256 FROM catalog_assets ORDER BY asset_sha256")?;
            loop {
                match unsafe { sqlite3_step(q.raw) } {
                    SQLITE_ROW => self.ensure_public_catalog_identity(&column_text(q.raw, 0)?)?,
                    SQLITE_DONE => break,
                    _ => return Err(sqlite_error(self.database)),
                }
            }
            Ok(())
        })();
        self.finish_transaction(result)
    }
    pub fn begin_public_catalog_boot_epoch(&self) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let result = (|| {
            self.validate_public_projection_infrastructure()?;
            self.execute("DROP TRIGGER public_discovery_generation_append;DROP TRIGGER public_discovery_generation_capacity;DROP TRIGGER public_discovery_generation_history_no_update;DROP TRIGGER public_discovery_generation_history_no_delete;DELETE FROM public_catalog_generation_history;UPDATE public_catalog_generation SET generation=1 WHERE singleton=1;INSERT INTO public_catalog_generation_history VALUES(1);CREATE TRIGGER public_discovery_generation_capacity BEFORE UPDATE ON public_catalog_generation WHEN NEW.generation>1000001 BEGIN SELECT RAISE(ABORT,'public catalog generation capacity exceeded');END;CREATE TRIGGER public_discovery_generation_append AFTER UPDATE ON public_catalog_generation BEGIN INSERT INTO public_catalog_generation_history(generation) VALUES(NEW.generation);END;CREATE TRIGGER public_discovery_generation_history_no_update BEFORE UPDATE ON public_catalog_generation_history BEGIN SELECT RAISE(ABORT,'public catalog generation history is append-only');END;CREATE TRIGGER public_discovery_generation_history_no_delete BEFORE DELETE ON public_catalog_generation_history BEGIN SELECT RAISE(ABORT,'public catalog generation history is append-only');END;")?;
            self.validate_public_projection_infrastructure()
        })();
        self.finish_transaction(result)
    }
    pub(super) fn ensure_public_catalog_identity(&self, asset: &str) -> Result<(), String> {
        let id = public_id(asset);
        let q=self.prepare("INSERT INTO public_catalog_identities(asset_sha256,public_id) VALUES(?1,?2) ON CONFLICT(asset_sha256) DO UPDATE SET public_id=public_id RETURNING public_id")?;
        let a = c_string(asset, "catalog identity")?;
        let p = c_string(&id, "public identity")?;
        bind_text(self.database, q.raw, 1, &a)?;
        bind_text(self.database, q.raw, 2, &p)?;
        match unsafe { sqlite3_step(q.raw) } {
            SQLITE_ROW if column_text(q.raw, 0)? == id => Ok(()),
            SQLITE_ROW => Err("public catalog identity mapping disagrees".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }
    pub fn read_public_catalog(
        &self,
        cursor: Option<&PublicCatalogCursor>,
        limit: usize,
        filters: &[PublicCatalogFilter],
    ) -> Result<PublicCatalogPage, String> {
        let mut filters = filters.to_vec();
        filters.sort_by(|left, right| left.field_key.cmp(&right.field_key));
        validate_query(limit, &filters)?;
        self.execute("BEGIN")?;
        let r = self.public_snapshot(cursor, limit, &filters);
        match r {
            Ok(v) => self.execute("COMMIT").map(|_| v).inspect_err(|_| {
                let _ = self.execute("ROLLBACK");
            }),
            Err(e) => {
                let _ = self.execute("ROLLBACK");
                Err(e)
            }
        }
    }
    fn public_snapshot(
        &self,
        cursor: Option<&PublicCatalogCursor>,
        limit: usize,
        filters: &[PublicCatalogFilter],
    ) -> Result<PublicCatalogPage, String> {
        self.validate_public_projection_infrastructure()?;
        validate_definitions(self, filters)?;
        let generation = self.public_generation()?;
        let fd = filter_digest(filters)?;
        let (time, id) = parse_cursor(cursor, generation, &fd)?;
        let q=self.prepare("SELECT c.asset_sha256,c.title,c.meeting_body,c.editorial_state,c.published_at_utc_ms,c.revision,c.record_json,p.public_id FROM catalog_assets c JOIN public_catalog_identities p ON p.asset_sha256=c.asset_sha256 WHERE c.editorial_state='published' AND (?1='' OR c.published_at_utc_ms<?2 OR(c.published_at_utc_ms=?2 AND p.public_id>?1)) ORDER BY c.published_at_utc_ms DESC,p.public_id LIMIT ?3")?;
        let ci = c_string(id.as_deref().unwrap_or_default(), "cursor")?;
        bind_text(self.database, q.raw, 1, &ci)?;
        bind_i64(self.database, q.raw, 2, time.unwrap_or(i64::MAX))?;
        bind_i64(self.database, q.raw, 3, (MAX_CANDIDATES + 1) as i64)?;
        let mut items = Vec::new();
        let mut examined = 0;
        let mut last = None;
        let mut more = false;
        loop {
            match unsafe { sqlite3_step(q.raw) } {
                SQLITE_ROW if examined == MAX_CANDIDATES => {
                    more = true;
                    break;
                }
                SQLITE_ROW => {
                    examined += 1;
                    let r = catalog_authority::read_catalog_asset_row(q.raw)?;
                    self.validate_current_catalog_snapshot(&r)?;
                    let pid = column_text(q.raw, 7)?;
                    if pid != public_id(&r.asset_sha256) {
                        return Err("public identity mapping corrupt".into());
                    }
                    let at = r.published_at_utc_ms.ok_or("published time missing")?;
                    last = Some((at, pid.clone()));
                    let m = self
                        .read_media_asset(&r.asset_sha256)?
                        .ok_or("published media missing")?;
                    if m.asset_id != r.asset_sha256 || m.readiness != AssetReadiness::Ready {
                        return Err("published media is not ready".into());
                    }
                    let facets = self.public_facets(&r.asset_sha256)?;
                    if filters.iter().all(|f| {
                        facets
                            .iter()
                            .any(|x| x.field_key == f.field_key && x.value == f.value)
                    }) {
                        items.push(PublicCatalogItem {
                            asset_id: pid,
                            title: r.title,
                            published_at_utc_ms: at,
                            duration_ms: m.duration_ms,
                            facets,
                        })
                    }
                    if items.len() == limit {
                        more = match unsafe { sqlite3_step(q.raw) } {
                            SQLITE_ROW => true,
                            SQLITE_DONE => false,
                            _ => return Err(sqlite_error(self.database)),
                        };
                        break;
                    }
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        let next = if more {
            last.map(|(t, i)| PublicCatalogCursor {
                version: 1,
                published_at_utc_ms: t,
                asset_id: i,
                filter_digest: fd,
                generation,
            })
        } else {
            None
        };
        Ok(PublicCatalogPage {
            items,
            next_cursor: next,
        })
    }
    fn public_facets(&self, asset: &str) -> Result<Vec<MetadataFacet>, String> {
        let mut out = Vec::new();
        let mut keys = HashSet::new();
        for r in self.list_asset_metadata_records(asset)? {
            let h = self
                .list_asset_metadata_record_history(&r.metadata_id, MAX_CUSTOM_METADATA_HISTORY)?;
            if h.len() != usize::try_from(r.revision).map_err(|_| "metadata revision overflow")?
                || h.last().map(|x| &x.record) != Some(&r)
            {
                return Err("metadata history mismatch".into());
            }
            let s = self
                .read_custom_metadata_schema_revision(&r.schema_id, r.schema_revision)?
                .ok_or("historical schema missing")?;
            for f in project_public_facets(&r, &s)? {
                if !keys.insert(f.field_key.clone()) {
                    return Err("duplicate public facet".into());
                }
                out.push(f)
            }
        }
        out.sort_by(|a, b| a.field_key.cmp(&b.field_key));
        Ok(out)
    }
    fn public_generation(&self) -> Result<u64, String> {
        let q = self.prepare("SELECT generation,(SELECT min(generation) FROM public_catalog_generation_history),(SELECT max(generation) FROM public_catalog_generation_history),(SELECT count(*) FROM public_catalog_generation_history) FROM public_catalog_generation WHERE singleton=1")?;
        match unsafe { sqlite3_step(q.raw) } {
            SQLITE_ROW => {
                let current = unsafe { sqlite3_column_int64(q.raw, 0) };
                let minimum = unsafe { sqlite3_column_int64(q.raw, 1) };
                let maximum = unsafe { sqlite3_column_int64(q.raw, 2) };
                let count = unsafe { sqlite3_column_int64(q.raw, 3) };
                if !(1..=1_000_001).contains(&current)
                    || minimum < 1
                    || maximum != current
                    || count != maximum - minimum + 1
                {
                    return Err("public catalog generation history is corrupt, gapped, or disagrees with current authority".into());
                }
                u64::try_from(current).map_err(|_| "generation corrupt".into())
            }
            _ => Err("generation missing".into()),
        }
    }
    fn validate_public_projection_infrastructure(&self) -> Result<(), String> {
        let mappings = self.prepare("SELECT c.asset_sha256,COALESCE(p.public_id,'') FROM catalog_assets c LEFT JOIN public_catalog_identities p ON p.asset_sha256=c.asset_sha256 ORDER BY c.asset_sha256 LIMIT 100001")?;
        let mut count = 0usize;
        loop {
            match unsafe { sqlite3_step(mappings.raw) } {
                SQLITE_ROW => {
                    count += 1;
                    let asset = column_text(mappings.raw, 0)?;
                    if column_text(mappings.raw, 1)? != public_id(&asset) {
                        return Err("public catalog identity mapping is missing or corrupt".into());
                    }
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        if count > catalog_authority::MAX_CATALOG_ASSETS {
            return Err("public catalog mapping is unbounded".into());
        }
        let orphans = self.prepare("SELECT count(*) FROM public_catalog_identities p LEFT JOIN catalog_assets c ON c.asset_sha256=p.asset_sha256 WHERE c.asset_sha256 IS NULL")?;
        if unsafe { sqlite3_step(orphans.raw) } != SQLITE_ROW
            || unsafe { sqlite3_column_int64(orphans.raw, 0) } != 0
        {
            return Err("public catalog identity mapping contains orphan rows".into());
        }
        self.validate_public_generation_triggers()
    }
    fn validate_public_generation_triggers(&self) -> Result<(), String> {
        const EXPECTED: &[(&str, &str, &str)] = &[
            (
                "public_discovery_catalog_h",
                "catalog_asset_history",
                "insert",
            ),
            ("public_discovery_catalog_i", "catalog_assets", "insert"),
            ("public_discovery_catalog_u", "catalog_assets", "update"),
            ("public_discovery_media_i", "media_assets", "insert"),
            ("public_discovery_media_u", "media_assets", "update"),
            (
                "public_discovery_metadata_h",
                "asset_metadata_record_history",
                "insert",
            ),
            (
                "public_discovery_metadata_i",
                "asset_metadata_records",
                "insert",
            ),
            (
                "public_discovery_metadata_u",
                "asset_metadata_records",
                "update",
            ),
            (
                "public_discovery_schema_h",
                "custom_metadata_schema_history",
                "insert",
            ),
            (
                "public_discovery_schema_i",
                "custom_metadata_schemas",
                "insert",
            ),
            (
                "public_discovery_schema_u",
                "custom_metadata_schemas",
                "update",
            ),
        ];
        let query = self.prepare("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'public_discovery_%' AND name NOT LIKE 'public_discovery_generation_%' ORDER BY name")?;
        for (name, table, event) in EXPECTED {
            if unsafe { sqlite3_step(query.raw) } != SQLITE_ROW
                || column_text(query.raw, 0)? != *name
                || column_text(query.raw, 1)? != *table
            {
                return Err("public catalog generation trigger set is corrupt".into());
            }
            let actual = column_text(query.raw, 2)?
                .split_whitespace()
                .collect::<String>()
                .to_ascii_lowercase();
            let expected = format!(
                "createtrigger{name}after{event}on{table}beginupdatepublic_catalog_generationsetgeneration=generation+1wheresingleton=1;end"
            );
            if actual != expected {
                return Err("public catalog generation trigger SQL is corrupt".into());
            }
        }
        if unsafe { sqlite3_step(query.raw) } != SQLITE_DONE {
            return Err("public catalog generation trigger set is unbounded".into());
        }
        self.validate_public_generation_history_triggers()
    }
    fn validate_public_generation_history_triggers(&self) -> Result<(), String> {
        const EXPECTED: &[(&str, &str)] = &[
            (
                "public_discovery_generation_append",
                "createtriggerpublic_discovery_generation_appendafterupdateonpublic_catalog_generationbegininsertintopublic_catalog_generation_history(generation)values(new.generation);end",
            ),
            (
                "public_discovery_generation_capacity",
                "createtriggerpublic_discovery_generation_capacitybeforeupdateonpublic_catalog_generationwhennew.generation>1000001beginselectraise(abort,'publiccataloggenerationcapacityexceeded');end",
            ),
            (
                "public_discovery_generation_history_no_delete",
                "createtriggerpublic_discovery_generation_history_no_deletebeforedeleteonpublic_catalog_generation_historybeginselectraise(abort,'publiccataloggenerationhistoryisappend-only');end",
            ),
            (
                "public_discovery_generation_history_no_update",
                "createtriggerpublic_discovery_generation_history_no_updatebeforeupdateonpublic_catalog_generation_historybeginselectraise(abort,'publiccataloggenerationhistoryisappend-only');end",
            ),
        ];
        let query=self.prepare("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'public_discovery_generation_%' ORDER BY name")?;
        for (name, expected) in EXPECTED {
            if unsafe { sqlite3_step(query.raw) } != SQLITE_ROW
                || column_text(query.raw, 0)? != *name
                || column_text(query.raw, 1)?
                    .split_whitespace()
                    .collect::<String>()
                    .to_ascii_lowercase()
                    != *expected
            {
                return Err("public catalog generation history trigger SQL is corrupt".into());
            }
        }
        if unsafe { sqlite3_step(query.raw) } != SQLITE_DONE {
            return Err("public catalog generation history trigger set is unbounded".into());
        }
        Ok(())
    }
}
fn validate_definitions(s: &StationStore, fs: &[PublicCatalogFilter]) -> Result<(), String> {
    let schemas = s.list_custom_metadata_schemas()?;
    let mut global = std::collections::HashMap::new();
    let mut count = 0usize;
    for schema in &schemas {
        let history =
            s.list_custom_metadata_schema_history(&schema.schema_id, MAX_CUSTOM_METADATA_HISTORY)?;
        if history.len()
            != usize::try_from(schema.revision).map_err(|_| "schema revision overflow")?
            || history.last().map(|entry| &entry.schema) != Some(schema)
        {
            return Err("current custom metadata schema disagrees with immutable history".into());
        }
        for field in schema
            .fields
            .iter()
            .filter(|field| field.public && field.faceted)
        {
            count += 1;
            if global
                .insert(field.key.as_str(), field_kind(&field.field_type))
                .is_some()
            {
                return Err("public facet key authority is duplicated across schemas".into());
            }
        }
    }
    if count > MAX_PUBLIC_CATALOG_FACETS {
        return Err("public facet authority exceeds 128 fields".into());
    }
    for f in fs {
        let m = schemas
            .iter()
            .flat_map(|s| &s.fields)
            .filter(|d| d.key == f.field_key && d.public && d.faceted)
            .collect::<Vec<_>>();
        if m.is_empty() || m.iter().any(|d| !kind(&d.field_type, &f.value)) {
            return Err("filter is unknown, private, non-faceted, or wrong type".into());
        }
    }
    Ok(())
}
fn kind(t: &MetadataFieldType, v: &MetadataProjectionValue) -> bool {
    matches!(
        (t, v),
        (
            MetadataFieldType::Text { .. },
            MetadataProjectionValue::Text(_)
        ) | (
            MetadataFieldType::Number { .. },
            MetadataProjectionValue::Number(_)
        ) | (
            MetadataFieldType::DateTime { .. },
            MetadataProjectionValue::DateTimeUnixMs(_)
        ) | (
            MetadataFieldType::Boolean,
            MetadataProjectionValue::Boolean(_)
        ) | (
            MetadataFieldType::ControlledChoice { .. },
            MetadataProjectionValue::ControlledChoice(_)
        )
    )
}
fn field_kind(t: &MetadataFieldType) -> u8 {
    match t {
        MetadataFieldType::Text { .. } => 0,
        MetadataFieldType::Number { .. } => 1,
        MetadataFieldType::DateTime { .. } => 2,
        MetadataFieldType::Boolean => 3,
        MetadataFieldType::ControlledChoice { .. } => 4,
    }
}
fn validate_query(l: usize, fs: &[PublicCatalogFilter]) -> Result<(), String> {
    let mut k = HashSet::new();
    if !(1..=100).contains(&l)
        || fs.len() > 8
        || fs.iter().any(|f| {
            f.field_key.is_empty()
                || f.field_key.len() > 64
                || !k.insert(f.field_key.as_str())
                || !bounded(&f.value)
        })
    {
        Err("public query invalid or unbounded".into())
    } else {
        Ok(())
    }
}
fn bounded(v: &MetadataProjectionValue) -> bool {
    match v {
        MetadataProjectionValue::Text(x) => {
            !x.is_empty()
                && x.len() <= 8192
                && x.chars().count() <= 2048
                && !x.chars().any(char::is_control)
        }
        MetadataProjectionValue::ControlledChoice(x) => {
            !x.is_empty()
                && x.len() <= 64
                && x.bytes().all(|b| {
                    b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'-')
                })
        }
        _ => true,
    }
}
fn public_id(a: &str) -> String {
    format!(
        "{:x}",
        Sha256::digest(format!("townlight-public-discovery-v1:{a}").as_bytes())
    )
}
fn filter_digest(f: &[PublicCatalogFilter]) -> Result<String, String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(f).map_err(|e| e.to_string())?)
    ))
}
fn parse_cursor(
    c: Option<&PublicCatalogCursor>,
    g: u64,
    f: &str,
) -> Result<(Option<i64>, Option<String>), String> {
    let Some(c) = c else { return Ok((None, None)) };
    if c.version != 1 || c.asset_id.len() != 64 || c.filter_digest != f || c.generation != g {
        return Err("public catalog cursor stale or invalid".into());
    }
    Ok((Some(c.published_at_utc_ms), Some(c.asset_id.clone())))
}
