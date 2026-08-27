//! Pure, fail-closed custom-metadata authority contracts.
//!
//! Persistence, API, authorization, indexing, and operator UI boundaries are
//! deliberately outside this crate.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

pub const MAX_METADATA_FIELDS: usize = 128;
pub const MAX_METADATA_VALUES_PER_ASSET: usize = MAX_METADATA_FIELDS;
pub const MAX_FIELD_KEY_BYTES: usize = 64;
pub const MAX_FIELD_LABEL_BYTES: usize = 120;
pub const MAX_FIELD_DESCRIPTION_BYTES: usize = 1_000;
pub const MAX_METADATA_TEXT_CHARS: u16 = 2_048;
pub const MAX_CONTROLLED_CHOICES: usize = 128;
pub const MAX_CHOICE_KEY_BYTES: usize = 64;
pub const MAX_CHOICE_LABEL_BYTES: usize = 120;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    rename_all = "snake_case",
    tag = "kind",
    content = "constraints",
    deny_unknown_fields
)]
pub enum MetadataFieldType {
    Text {
        min_chars: u16,
        max_chars: u16,
    },
    Number {
        minimum: i64,
        maximum: i64,
    },
    DateTime {
        minimum_unix_ms: Option<i64>,
        maximum_unix_ms: Option<i64>,
    },
    Boolean,
    ControlledChoice {
        choices: Vec<ControlledChoice>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlledChoice {
    pub choice_id: String,
    pub key: String,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MetadataFieldDefinition {
    pub field_id: String,
    pub key: String,
    pub label: String,
    pub description: Option<String>,
    pub required: bool,
    pub public: bool,
    pub faceted: bool,
    pub field_type: MetadataFieldType,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MetadataSchema {
    pub schema_id: String,
    pub revision: u64,
    pub fields: Vec<MetadataFieldDefinition>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    rename_all = "snake_case",
    tag = "kind",
    content = "value",
    deny_unknown_fields
)]
pub enum MetadataFieldValue {
    Text(String),
    Number(i64),
    DateTimeUnixMs(i64),
    Boolean(bool),
    ControlledChoice(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssetMetadataValue {
    pub field_id: String,
    pub value: MetadataFieldValue,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssetMetadataRecord {
    pub metadata_id: String,
    pub asset_id: String,
    pub schema_id: String,
    pub schema_revision: u64,
    pub revision: u64,
    pub values: Vec<AssetMetadataValue>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    rename_all = "snake_case",
    tag = "kind",
    content = "value",
    deny_unknown_fields
)]
pub enum MetadataProjectionValue {
    Text(String),
    Number(i64),
    DateTimeUnixMs(i64),
    Boolean(bool),
    ControlledChoice(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MetadataFacet {
    pub field_key: String,
    pub value: MetadataProjectionValue,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicMetadataValue {
    pub field_key: String,
    pub value: MetadataProjectionValue,
}

impl MetadataSchema {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.schema_id, "metadata schema identity")?;
        if self.revision == 0 || self.fields.is_empty() || self.fields.len() > MAX_METADATA_FIELDS {
            return Err("metadata schema revision or field count is invalid or unbounded".into());
        }
        let mut field_ids = HashSet::with_capacity(self.fields.len());
        let mut keys = HashSet::with_capacity(self.fields.len());
        for field in &self.fields {
            field.validate()?;
            if !field_ids.insert(field.field_id.as_str()) || !keys.insert(field.key.as_str()) {
                return Err("metadata field identities and keys must be unique".into());
            }
        }
        Ok(())
    }
}

impl MetadataFieldDefinition {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.field_id, "metadata field identity")?;
        validate_key(&self.key, MAX_FIELD_KEY_BYTES, "metadata field key")?;
        validate_visible(&self.label, MAX_FIELD_LABEL_BYTES, "metadata field label")?;
        if let Some(description) = &self.description {
            validate_visible(
                description,
                MAX_FIELD_DESCRIPTION_BYTES,
                "metadata field description",
            )?;
        }
        self.field_type.validate()
    }
}

impl MetadataFieldType {
    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::Text {
                min_chars,
                max_chars,
            } if *min_chars <= *max_chars && *max_chars <= MAX_METADATA_TEXT_CHARS => Ok(()),
            Self::Number { minimum, maximum } if minimum <= maximum => Ok(()),
            Self::DateTime {
                minimum_unix_ms,
                maximum_unix_ms,
            } if minimum_unix_ms
                .zip(*maximum_unix_ms)
                .is_none_or(|(minimum, maximum)| minimum <= maximum) =>
            {
                Ok(())
            }
            Self::Boolean => Ok(()),
            Self::ControlledChoice { choices }
                if !choices.is_empty() && choices.len() <= MAX_CONTROLLED_CHOICES =>
            {
                let mut choice_ids = HashSet::with_capacity(choices.len());
                let mut keys = HashSet::with_capacity(choices.len());
                for choice in choices {
                    choice.validate()?;
                    if !choice_ids.insert(choice.choice_id.as_str())
                        || !keys.insert(choice.key.as_str())
                    {
                        return Err("controlled choice identities and keys must be unique".into());
                    }
                }
                Ok(())
            }
            _ => Err("metadata field constraints are invalid or unbounded".into()),
        }
    }

    fn stable_kind(&self) -> &'static str {
        match self {
            Self::Text { .. } => "text",
            Self::Number { .. } => "number",
            Self::DateTime { .. } => "date_time",
            Self::Boolean => "boolean",
            Self::ControlledChoice { .. } => "controlled_choice",
        }
    }
}

impl ControlledChoice {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.choice_id, "controlled choice identity")?;
        validate_key(&self.key, MAX_CHOICE_KEY_BYTES, "controlled choice key")?;
        validate_visible(
            &self.label,
            MAX_CHOICE_LABEL_BYTES,
            "controlled choice label",
        )
    }
}

impl AssetMetadataRecord {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.metadata_id, "asset metadata identity")?;
        if !is_sha256(&self.asset_id)
            || !is_canonical_uuid(&self.schema_id)
            || self.schema_revision == 0
            || self.revision == 0
            || self.values.len() > MAX_METADATA_VALUES_PER_ASSET
        {
            return Err(
                "asset metadata identity, revision, or value count is invalid or unbounded".into(),
            );
        }
        Ok(())
    }
}

/// Validates creation or compare-and-set replacement of a schema. Existing
/// field and controlled-choice identities cannot be rebound to a different key
/// or type; a later persistence boundary can add audited retirement semantics.
pub fn validate_schema_write(
    current: Option<&MetadataSchema>,
    expected_revision: Option<u64>,
    proposed: &MetadataSchema,
) -> Result<(), String> {
    proposed.validate()?;
    let Some(current) = current else {
        if expected_revision.is_none() && proposed.revision == 1 {
            return Ok(());
        }
        return Err(
            "metadata schema creation requires revision one and no expected revision".into(),
        );
    };
    current.validate()?;
    if expected_revision != Some(current.revision)
        || current.revision.checked_add(1) != Some(proposed.revision)
    {
        return Err("metadata schema write has a stale or invalid compare-and-set revision".into());
    }
    if proposed.schema_id != current.schema_id {
        return Err("metadata schema identity cannot be rebound".into());
    }
    for prior in &current.fields {
        let Some(next) = proposed
            .fields
            .iter()
            .find(|field| field.field_id == prior.field_id)
        else {
            return Err("metadata schema updates cannot remove an existing field identity".into());
        };
        if next.key != prior.key || next.field_type.stable_kind() != prior.field_type.stable_kind()
        {
            return Err("metadata field identity cannot be rebound to another key or type".into());
        }
        validate_choice_identity_stability(&prior.field_type, &next.field_type)?;
    }
    Ok(())
}

/// Validates one exact schema-revision value record. Callers must provide the
/// revision that was current when the values were authored; guessing a schema
/// from the latest definition is intentionally not supported.
pub fn validate_asset_metadata_record(
    record: &AssetMetadataRecord,
    schema: &MetadataSchema,
) -> Result<(), String> {
    record.validate_shape()?;
    schema.validate()?;
    if record.schema_id != schema.schema_id || record.schema_revision != schema.revision {
        return Err("asset metadata values must bind to the exact schema revision".into());
    }
    let fields = schema
        .fields
        .iter()
        .map(|field| (field.field_id.as_str(), field))
        .collect::<HashMap<_, _>>();
    let mut supplied = HashSet::with_capacity(record.values.len());
    for value in &record.values {
        if !supplied.insert(value.field_id.as_str()) {
            return Err("asset metadata values must not repeat a field identity".into());
        }
        let Some(field) = fields.get(value.field_id.as_str()) else {
            return Err("asset metadata value references an unknown schema field".into());
        };
        validate_value(field, &value.value)?;
    }
    if schema
        .fields
        .iter()
        .any(|field| field.required && !supplied.contains(field.field_id.as_str()))
    {
        return Err("asset metadata values omit a required schema field".into());
    }
    Ok(())
}

pub fn validate_asset_metadata_write(
    current: Option<&AssetMetadataRecord>,
    expected_revision: Option<u64>,
    proposed: &AssetMetadataRecord,
    schema: &MetadataSchema,
) -> Result<(), String> {
    validate_asset_metadata_record(proposed, schema)?;
    let Some(current) = current else {
        if expected_revision.is_none() && proposed.revision == 1 {
            return Ok(());
        }
        return Err(
            "asset metadata creation requires revision one and no expected revision".into(),
        );
    };
    current.validate_shape()?;
    if expected_revision != Some(current.revision)
        || current.revision.checked_add(1) != Some(proposed.revision)
    {
        return Err("asset metadata write has a stale or invalid compare-and-set revision".into());
    }
    if proposed.metadata_id != current.metadata_id
        || proposed.asset_id != current.asset_id
        || proposed.schema_id != current.schema_id
    {
        return Err("asset metadata identity cannot be rebound".into());
    }
    Ok(())
}

pub fn project_search_facets(
    record: &AssetMetadataRecord,
    schema: &MetadataSchema,
) -> Result<Vec<MetadataFacet>, String> {
    validate_asset_metadata_record(record, schema)?;
    project_values(record, schema, |field| field.faceted).map(|values| {
        values
            .into_iter()
            .map(|value| MetadataFacet {
                field_key: value.field_key,
                value: value.value,
            })
            .collect()
    })
}

pub fn project_public_metadata(
    record: &AssetMetadataRecord,
    schema: &MetadataSchema,
) -> Result<Vec<PublicMetadataValue>, String> {
    validate_asset_metadata_record(record, schema)?;
    project_values(record, schema, |field| field.public).map(|values| {
        values
            .into_iter()
            .map(|value| PublicMetadataValue {
                field_key: value.field_key,
                value: value.value,
            })
            .collect()
    })
}

/// Projects only values explicitly approved for both public disclosure and
/// exact-match faceting. Internal search keeps its broader `faceted` contract.
pub fn project_public_facets(
    record: &AssetMetadataRecord,
    schema: &MetadataSchema,
) -> Result<Vec<MetadataFacet>, String> {
    validate_asset_metadata_record(record, schema)?;
    project_values(record, schema, |field| field.public && field.faceted).map(|values| {
        values
            .into_iter()
            .map(|value| MetadataFacet {
                field_key: value.field_key,
                value: value.value,
            })
            .collect()
    })
}

struct ProjectedValue {
    field_key: String,
    value: MetadataProjectionValue,
}

fn project_values(
    record: &AssetMetadataRecord,
    schema: &MetadataSchema,
    include: impl Fn(&MetadataFieldDefinition) -> bool,
) -> Result<Vec<ProjectedValue>, String> {
    let fields = schema
        .fields
        .iter()
        .map(|field| (field.field_id.as_str(), field))
        .collect::<HashMap<_, _>>();
    let mut projected = Vec::new();
    for asset_value in &record.values {
        let field = fields
            .get(asset_value.field_id.as_str())
            .ok_or_else(|| "asset metadata value references an unknown schema field".to_string())?;
        if include(field) {
            projected.push(ProjectedValue {
                field_key: field.key.clone(),
                value: project_value(&field.field_type, &asset_value.value)?,
            });
        }
    }
    projected.sort_by(|left, right| left.field_key.cmp(&right.field_key));
    Ok(projected)
}

fn project_value(
    field_type: &MetadataFieldType,
    value: &MetadataFieldValue,
) -> Result<MetadataProjectionValue, String> {
    match (field_type, value) {
        (MetadataFieldType::Text { .. }, MetadataFieldValue::Text(value)) => {
            Ok(MetadataProjectionValue::Text(value.clone()))
        }
        (MetadataFieldType::Number { .. }, MetadataFieldValue::Number(value)) => {
            Ok(MetadataProjectionValue::Number(*value))
        }
        (MetadataFieldType::DateTime { .. }, MetadataFieldValue::DateTimeUnixMs(value)) => {
            Ok(MetadataProjectionValue::DateTimeUnixMs(*value))
        }
        (MetadataFieldType::Boolean, MetadataFieldValue::Boolean(value)) => {
            Ok(MetadataProjectionValue::Boolean(*value))
        }
        (
            MetadataFieldType::ControlledChoice { choices },
            MetadataFieldValue::ControlledChoice(choice_id),
        ) => {
            let choice = choices
                .iter()
                .find(|choice| choice.choice_id == *choice_id)
                .ok_or_else(|| {
                    "asset metadata controlled choice is not in the schema".to_string()
                })?;
            Ok(MetadataProjectionValue::ControlledChoice(
                choice.key.clone(),
            ))
        }
        _ => Err("asset metadata value type does not match its schema field".into()),
    }
}

fn validate_value(
    field: &MetadataFieldDefinition,
    value: &MetadataFieldValue,
) -> Result<(), String> {
    match (&field.field_type, value) {
        (
            MetadataFieldType::Text {
                min_chars,
                max_chars,
            },
            MetadataFieldValue::Text(value),
        ) => {
            let count = value.chars().count();
            if value.trim() != value
                || value.chars().any(char::is_control)
                || count < usize::from(*min_chars)
                || count > usize::from(*max_chars)
            {
                return Err("asset metadata text value violates its schema constraints".into());
            }
            Ok(())
        }
        (MetadataFieldType::Number { minimum, maximum }, MetadataFieldValue::Number(value))
            if value >= minimum && value <= maximum =>
        {
            Ok(())
        }
        (
            MetadataFieldType::DateTime {
                minimum_unix_ms,
                maximum_unix_ms,
            },
            MetadataFieldValue::DateTimeUnixMs(value),
        ) if minimum_unix_ms.is_none_or(|minimum| value >= &minimum)
            && maximum_unix_ms.is_none_or(|maximum| value <= &maximum) =>
        {
            Ok(())
        }
        (MetadataFieldType::Boolean, MetadataFieldValue::Boolean(_)) => Ok(()),
        (
            MetadataFieldType::ControlledChoice { choices },
            MetadataFieldValue::ControlledChoice(choice_id),
        ) if choices.iter().any(|choice| choice.choice_id == *choice_id) => Ok(()),
        _ => Err("asset metadata value type or constraint does not match its schema field".into()),
    }
}

fn validate_choice_identity_stability(
    current: &MetadataFieldType,
    proposed: &MetadataFieldType,
) -> Result<(), String> {
    let (
        MetadataFieldType::ControlledChoice { choices: current },
        MetadataFieldType::ControlledChoice { choices: proposed },
    ) = (current, proposed)
    else {
        return Ok(());
    };
    for prior in current {
        let Some(next) = proposed
            .iter()
            .find(|choice| choice.choice_id == prior.choice_id)
        else {
            return Err(
                "metadata schema updates cannot remove a controlled choice identity".into(),
            );
        };
        if next.key != prior.key {
            return Err("controlled choice identity cannot be rebound to another key".into());
        }
    }
    Ok(())
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    if !is_canonical_uuid(value) {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn is_canonical_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            matches!(index, 8 | 13 | 18 | 23)
                || byte.is_ascii_digit()
                || (b'a'..=b'f').contains(byte)
        })
}

fn validate_key(value: &str, maximum: usize, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.is_empty()
        || bytes.len() > maximum
        || !bytes[0].is_ascii_lowercase()
        || !bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_')
    {
        return Err(format!(
            "{field} must be lowercase ASCII snake_case and bounded"
        ));
    }
    Ok(())
}

fn validate_visible(value: &str, maximum: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCHEMA_ID: &str = "11111111-1111-4111-8111-111111111111";
    const ASSET_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn choice() -> ControlledChoice {
        ControlledChoice {
            choice_id: "22222222-2222-4222-8222-222222222222".into(),
            key: "meeting".into(),
            label: "Meeting".into(),
        }
    }

    fn field(id: &str, key: &str, field_type: MetadataFieldType) -> MetadataFieldDefinition {
        MetadataFieldDefinition {
            field_id: id.into(),
            key: key.into(),
            label: key.into(),
            description: None,
            required: true,
            public: key != "internal_note",
            faceted: key != "internal_note",
            field_type,
        }
    }

    fn schema(revision: u64) -> MetadataSchema {
        MetadataSchema {
            schema_id: SCHEMA_ID.into(),
            revision,
            fields: vec![
                field(
                    "33333333-3333-4333-8333-333333333333",
                    "air_date",
                    MetadataFieldType::DateTime {
                        minimum_unix_ms: Some(0),
                        maximum_unix_ms: Some(4_102_444_800_000),
                    },
                ),
                field(
                    "44444444-4444-4444-8444-444444444444",
                    "episode",
                    MetadataFieldType::Number {
                        minimum: 1,
                        maximum: 9999,
                    },
                ),
                field(
                    "55555555-5555-4555-8555-555555555555",
                    "featured",
                    MetadataFieldType::Boolean,
                ),
                field(
                    "66666666-6666-4666-8666-666666666666",
                    "format",
                    MetadataFieldType::ControlledChoice {
                        choices: vec![choice()],
                    },
                ),
                field(
                    "77777777-7777-4777-8777-777777777777",
                    "title",
                    MetadataFieldType::Text {
                        min_chars: 1,
                        max_chars: 80,
                    },
                ),
            ],
        }
    }

    fn record(schema_revision: u64) -> AssetMetadataRecord {
        AssetMetadataRecord {
            metadata_id: "88888888-8888-4888-8888-888888888888".into(),
            asset_id: ASSET_ID.into(),
            schema_id: SCHEMA_ID.into(),
            schema_revision,
            revision: 1,
            values: vec![
                AssetMetadataValue {
                    field_id: "77777777-7777-4777-8777-777777777777".into(),
                    value: MetadataFieldValue::Text("Council Meeting".into()),
                },
                AssetMetadataValue {
                    field_id: "55555555-5555-4555-8555-555555555555".into(),
                    value: MetadataFieldValue::Boolean(true),
                },
                AssetMetadataValue {
                    field_id: "44444444-4444-4444-8444-444444444444".into(),
                    value: MetadataFieldValue::Number(42),
                },
                AssetMetadataValue {
                    field_id: "33333333-3333-4333-8333-333333333333".into(),
                    value: MetadataFieldValue::DateTimeUnixMs(1_700_000_000_000),
                },
                AssetMetadataValue {
                    field_id: "66666666-6666-4666-8666-666666666666".into(),
                    value: MetadataFieldValue::ControlledChoice(choice().choice_id),
                },
            ],
        }
    }

    #[test]
    fn validates_typed_asset_values_and_projects_deterministic_public_facets() {
        let schema = schema(1);
        let record = record(1);
        validate_asset_metadata_record(&record, &schema).unwrap();
        assert_eq!(
            project_search_facets(&record, &schema).unwrap(),
            vec![
                MetadataFacet {
                    field_key: "air_date".into(),
                    value: MetadataProjectionValue::DateTimeUnixMs(1_700_000_000_000)
                },
                MetadataFacet {
                    field_key: "episode".into(),
                    value: MetadataProjectionValue::Number(42)
                },
                MetadataFacet {
                    field_key: "featured".into(),
                    value: MetadataProjectionValue::Boolean(true)
                },
                MetadataFacet {
                    field_key: "format".into(),
                    value: MetadataProjectionValue::ControlledChoice("meeting".into())
                },
                MetadataFacet {
                    field_key: "title".into(),
                    value: MetadataProjectionValue::Text("Council Meeting".into())
                },
            ]
        );
        assert_eq!(project_public_metadata(&record, &schema).unwrap().len(), 5);
    }

    #[test]
    fn rejects_wrong_type_out_of_range_and_unknown_schema_values() {
        let schema = schema(1);
        let mut wrong_type = record(1);
        wrong_type.values[0].value = MetadataFieldValue::Number(4);
        assert!(validate_asset_metadata_record(&wrong_type, &schema).is_err());
        let mut out_of_range = record(1);
        out_of_range.values[2].value = MetadataFieldValue::Number(10_000);
        assert!(validate_asset_metadata_record(&out_of_range, &schema).is_err());
        let mut unknown = record(1);
        unknown.values[0].field_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_asset_metadata_record(&unknown, &schema).is_err());
    }

    #[test]
    fn projects_only_public_and_faceted_values_in_stable_key_order() {
        let mut schema = schema(1);
        schema.fields[0].public = false;
        schema.fields[1].faceted = false;
        let record = record(1);
        let facets = project_search_facets(&record, &schema).unwrap();
        let public = project_public_metadata(&record, &schema).unwrap();
        let public_facets = project_public_facets(&record, &schema).unwrap();
        assert_eq!(facets.len(), 4);
        assert_eq!(public.len(), 4);
        assert!(facets.iter().all(|facet| facet.field_key != "episode"));
        assert!(public.iter().all(|value| value.field_key != "air_date"));
        assert_eq!(public_facets.len(), 3);
        assert!(
            public_facets
                .iter()
                .all(|facet| facet.field_key != "air_date" && facet.field_key != "episode")
        );
        assert!(
            facets
                .windows(2)
                .all(|pair| pair[0].field_key < pair[1].field_key)
        );
    }

    #[test]
    fn schema_writes_require_cas_and_forbid_identity_rebinding() {
        let current = schema(1);
        let mut proposed = schema(2);
        assert!(validate_schema_write(Some(&current), Some(1), &proposed).is_ok());
        assert!(validate_schema_write(Some(&current), Some(2), &proposed).is_err());
        proposed.fields[0].key = "broadcast_date".into();
        assert!(validate_schema_write(Some(&current), Some(1), &proposed).is_err());
        let mut retyped = schema(2);
        retyped.fields[0].field_type = MetadataFieldType::Text {
            min_chars: 1,
            max_chars: 10,
        };
        assert!(validate_schema_write(Some(&current), Some(1), &retyped).is_err());
    }

    #[test]
    fn asset_writes_bind_exact_schema_revision_and_preserve_asset_identity() {
        let schema = schema(1);
        let current = record(1);
        assert!(validate_asset_metadata_write(None, None, &current, &schema).is_ok());
        assert!(validate_asset_metadata_write(None, Some(1), &current, &schema).is_err());
        let mut update = record(1);
        update.revision = 2;
        assert!(validate_asset_metadata_write(Some(&current), Some(1), &update, &schema).is_ok());
        update.asset_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into();
        assert!(validate_asset_metadata_write(Some(&current), Some(1), &update, &schema).is_err());
        assert!(validate_asset_metadata_record(&record(2), &schema).is_err());
    }

    #[test]
    fn rejects_unknown_serde_fields_and_empty_or_unbounded_contracts() {
        assert!(serde_json::from_str::<MetadataSchema>(
            r#"{"schema_id":"11111111-1111-4111-8111-111111111111","revision":1,"fields":[],"unexpected":true}"#
        ).is_err());
        let mut invalid = schema(1);
        invalid.fields[4].field_type = MetadataFieldType::Text {
            min_chars: 3,
            max_chars: 2,
        };
        assert!(invalid.validate().is_err());
        let mut empty = record(1);
        empty.values.clear();
        assert!(validate_asset_metadata_record(&empty, &schema(1)).is_err());
        let mut too_many = record(1);
        too_many.values = vec![too_many.values[0].clone(); MAX_METADATA_VALUES_PER_ASSET + 1];
        assert!(validate_asset_metadata_record(&too_many, &schema(1)).is_err());
    }
}
