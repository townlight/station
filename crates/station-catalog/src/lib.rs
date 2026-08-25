//! Pure fixed catalog authority for station media.
//!
//! This crate deliberately contains no filesystem path, ingest job, or media
//! inspection fields. Editorial title, meeting-body taxonomy, and publication
//! state must be written explicitly by a trusted catalog boundary; they must
//! never be inferred from a filename, directory, or ingest completion time.

use serde::{Deserialize, Serialize};

pub const MAX_CATALOG_TITLE_BYTES: usize = 240;
/// UTF-8 byte limits intentionally match the scheduling candidate boundary.
/// They are byte, rather than scalar-value, limits so serialized authority has
/// one deterministic capacity rule for every language.
pub const MAX_MEETING_BODY_BYTES: usize = 240;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CatalogEditorialState {
    Draft,
    Published,
    Withdrawn,
}

/// Revisioned editorial authority for one immutable, content-addressed asset.
///
/// `published_at_utc_ms` is the time of first publication. Withdrawal does not
/// erase it, and republishing does not replace it. This makes `newest` ordering
/// stable across reversible editorial withdrawal. A future requirement for a
/// distinct republication time must add a separate field rather than silently
/// changing the meaning of this one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogAssetRecord {
    pub asset_sha256: String,
    pub title: String,
    pub meeting_body: Option<String>,
    pub editorial_state: CatalogEditorialState,
    pub published_at_utc_ms: Option<i64>,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CatalogAuthorityError {
    InvalidAssetIdentity,
    InvalidTitle,
    InvalidMeetingBody,
    InvalidRevision,
    InvalidOccurrenceTime,
    InvalidPublicationState,
    InvalidCreation,
    RevisionConflict {
        expected_revision: u64,
        current_revision: u64,
    },
    RevisionOverflow,
    IdentityRebound,
    InvalidEditorialTransition {
        from: CatalogEditorialState,
        to: CatalogEditorialState,
    },
    PublicationTimeRebound,
}

impl CatalogAssetRecord {
    pub fn validate(&self) -> Result<(), CatalogAuthorityError> {
        if !is_lowercase_sha256(&self.asset_sha256) {
            return Err(CatalogAuthorityError::InvalidAssetIdentity);
        }
        if !is_bounded_visible(&self.title, MAX_CATALOG_TITLE_BYTES) {
            return Err(CatalogAuthorityError::InvalidTitle);
        }
        if self
            .meeting_body
            .as_ref()
            .is_some_and(|value| !is_bounded_visible(value, MAX_MEETING_BODY_BYTES))
        {
            return Err(CatalogAuthorityError::InvalidMeetingBody);
        }
        if self.revision == 0 {
            return Err(CatalogAuthorityError::InvalidRevision);
        }
        match (self.editorial_state, self.published_at_utc_ms) {
            (CatalogEditorialState::Draft, None) => Ok(()),
            (
                CatalogEditorialState::Published | CatalogEditorialState::Withdrawn,
                Some(published_at_utc_ms),
            ) if published_at_utc_ms >= 0 => Ok(()),
            _ => Err(CatalogAuthorityError::InvalidPublicationState),
        }
    }

    /// Validates a new authority row. New catalog records always begin as a
    /// draft; publication is a separate, timestamp-bound successor event.
    pub fn validate_creation(&self) -> Result<(), CatalogAuthorityError> {
        self.validate()?;
        if self.revision != 1
            || self.editorial_state != CatalogEditorialState::Draft
            || self.published_at_utc_ms.is_some()
        {
            return Err(CatalogAuthorityError::InvalidCreation);
        }
        Ok(())
    }

    /// Validates one compare-and-set successor at a trusted server occurrence
    /// time. The first Draft→Published transition must use that exact time.
    /// Every later Published/Withdrawn transition preserves the original time.
    pub fn validate_successor_of(
        &self,
        current: &Self,
        expected_revision: u64,
        occurred_at_utc_ms: i64,
    ) -> Result<(), CatalogAuthorityError> {
        current.validate()?;
        self.validate()?;
        if occurred_at_utc_ms < 0 {
            return Err(CatalogAuthorityError::InvalidOccurrenceTime);
        }
        if current
            .published_at_utc_ms
            .is_some_and(|published_at| occurred_at_utc_ms < published_at)
        {
            return Err(CatalogAuthorityError::InvalidOccurrenceTime);
        }
        if expected_revision != current.revision {
            return Err(CatalogAuthorityError::RevisionConflict {
                expected_revision,
                current_revision: current.revision,
            });
        }
        let next_revision = current
            .revision
            .checked_add(1)
            .ok_or(CatalogAuthorityError::RevisionOverflow)?;
        if self.revision != next_revision {
            return Err(CatalogAuthorityError::RevisionConflict {
                expected_revision,
                current_revision: current.revision,
            });
        }
        if self.asset_sha256 != current.asset_sha256 {
            return Err(CatalogAuthorityError::IdentityRebound);
        }

        match (current.editorial_state, self.editorial_state) {
            (CatalogEditorialState::Draft, CatalogEditorialState::Draft) => Ok(()),
            (CatalogEditorialState::Draft, CatalogEditorialState::Published) => {
                if self.published_at_utc_ms == Some(occurred_at_utc_ms) {
                    Ok(())
                } else {
                    Err(CatalogAuthorityError::PublicationTimeRebound)
                }
            }
            (
                CatalogEditorialState::Published,
                CatalogEditorialState::Published | CatalogEditorialState::Withdrawn,
            )
            | (
                CatalogEditorialState::Withdrawn,
                CatalogEditorialState::Published | CatalogEditorialState::Withdrawn,
            ) => {
                if self.published_at_utc_ms == current.published_at_utc_ms {
                    Ok(())
                } else {
                    Err(CatalogAuthorityError::PublicationTimeRebound)
                }
            }
            (from, to) => Err(CatalogAuthorityError::InvalidEditorialTransition { from, to }),
        }
    }

    pub fn is_published(&self) -> bool {
        self.editorial_state == CatalogEditorialState::Published
    }
}

fn is_lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_bounded_visible(value: &str, maximum_bytes: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum_bytes
        && value == value.trim()
        && value.chars().all(|character| !character.is_control())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn draft() -> CatalogAssetRecord {
        CatalogAssetRecord {
            asset_sha256: "a".repeat(64),
            title: "City Council".into(),
            meeting_body: Some("City Council".into()),
            editorial_state: CatalogEditorialState::Draft,
            published_at_utc_ms: None,
            revision: 1,
        }
    }

    #[test]
    fn creation_is_an_explicit_bounded_draft() {
        let record = draft();
        assert_eq!(record.validate_creation(), Ok(()));
        assert!(!record.is_published());

        let mut published_creation = record.clone();
        published_creation.editorial_state = CatalogEditorialState::Published;
        published_creation.published_at_utc_ms = Some(10);
        assert_eq!(
            published_creation.validate_creation(),
            Err(CatalogAuthorityError::InvalidCreation)
        );

        let mut late_revision = record;
        late_revision.revision = 2;
        assert_eq!(
            late_revision.validate_creation(),
            Err(CatalogAuthorityError::InvalidCreation)
        );
    }

    #[test]
    fn first_publication_is_bound_to_the_trusted_occurrence_time() {
        let current = draft();
        let mut published = current.clone();
        published.editorial_state = CatalogEditorialState::Published;
        published.published_at_utc_ms = Some(100);
        published.revision = 2;
        assert_eq!(published.validate_successor_of(&current, 1, 100), Ok(()));
        assert!(published.is_published());

        published.published_at_utc_ms = Some(99);
        assert_eq!(
            published.validate_successor_of(&current, 1, 100),
            Err(CatalogAuthorityError::PublicationTimeRebound)
        );
    }

    #[test]
    fn withdrawal_and_republication_preserve_original_publication_time() {
        let current = draft();
        let mut published = current.clone();
        published.editorial_state = CatalogEditorialState::Published;
        published.published_at_utc_ms = Some(100);
        published.revision = 2;

        let mut withdrawn = published.clone();
        withdrawn.editorial_state = CatalogEditorialState::Withdrawn;
        withdrawn.revision = 3;
        assert_eq!(withdrawn.validate_successor_of(&published, 2, 200), Ok(()));
        assert!(!withdrawn.is_published());

        let mut republished = withdrawn.clone();
        republished.editorial_state = CatalogEditorialState::Published;
        republished.revision = 4;
        assert_eq!(
            republished.validate_successor_of(&withdrawn, 3, 300),
            Ok(())
        );
        assert_eq!(republished.published_at_utc_ms, Some(100));

        republished.published_at_utc_ms = Some(300);
        assert_eq!(
            republished.validate_successor_of(&withdrawn, 3, 300),
            Err(CatalogAuthorityError::PublicationTimeRebound)
        );
    }

    #[test]
    fn publication_history_cannot_return_to_draft_or_skip_publication() {
        let current = draft();
        let mut withdrawn = current.clone();
        withdrawn.editorial_state = CatalogEditorialState::Withdrawn;
        withdrawn.published_at_utc_ms = Some(10);
        withdrawn.revision = 2;
        assert_eq!(
            withdrawn.validate_successor_of(&current, 1, 10),
            Err(CatalogAuthorityError::InvalidEditorialTransition {
                from: CatalogEditorialState::Draft,
                to: CatalogEditorialState::Withdrawn,
            })
        );

        let mut published = current;
        published.editorial_state = CatalogEditorialState::Published;
        published.published_at_utc_ms = Some(10);
        published.revision = 2;
        let mut reset = published.clone();
        reset.editorial_state = CatalogEditorialState::Draft;
        reset.published_at_utc_ms = None;
        reset.revision = 3;
        assert_eq!(
            reset.validate_successor_of(&published, 2, 20),
            Err(CatalogAuthorityError::InvalidEditorialTransition {
                from: CatalogEditorialState::Published,
                to: CatalogEditorialState::Draft,
            })
        );
    }

    #[test]
    fn post_publication_changes_cannot_precede_first_publication() {
        let mut published = draft();
        published.editorial_state = CatalogEditorialState::Published;
        published.published_at_utc_ms = Some(100);
        published.revision = 2;
        let mut withdrawn = published.clone();
        withdrawn.editorial_state = CatalogEditorialState::Withdrawn;
        withdrawn.revision = 3;
        assert_eq!(
            withdrawn.validate_successor_of(&published, 2, 99),
            Err(CatalogAuthorityError::InvalidOccurrenceTime)
        );
    }

    #[test]
    fn successor_enforces_cas_and_immutable_content_identity() {
        let current = draft();
        let mut next = current.clone();
        next.title = "City Council meeting".into();
        next.revision = 2;
        assert_eq!(next.validate_successor_of(&current, 1, 10), Ok(()));
        assert_eq!(
            next.validate_successor_of(&current, 0, 10),
            Err(CatalogAuthorityError::RevisionConflict {
                expected_revision: 0,
                current_revision: 1,
            })
        );

        next.asset_sha256 = "b".repeat(64);
        assert_eq!(
            next.validate_successor_of(&current, 1, 10),
            Err(CatalogAuthorityError::IdentityRebound)
        );
    }

    #[test]
    fn malformed_or_unbounded_editorial_fields_fail_closed() {
        let mut record = draft();
        record.asset_sha256 = "A".repeat(64);
        assert_eq!(
            record.validate(),
            Err(CatalogAuthorityError::InvalidAssetIdentity)
        );

        record = draft();
        record.title = format!(" {}", "x".repeat(MAX_CATALOG_TITLE_BYTES));
        assert_eq!(record.validate(), Err(CatalogAuthorityError::InvalidTitle));

        record = draft();
        record.meeting_body = Some("\nCity Council".into());
        assert_eq!(
            record.validate(),
            Err(CatalogAuthorityError::InvalidMeetingBody)
        );

        record = draft();
        record.revision = 0;
        assert_eq!(
            record.validate(),
            Err(CatalogAuthorityError::InvalidRevision)
        );
    }

    #[test]
    fn serialized_authority_is_strict_and_uses_explicit_state_names() {
        let json = serde_json::to_value(draft()).unwrap();
        assert_eq!(json["editorial_state"], "draft");
        let mut object = json.as_object().unwrap().clone();
        object.insert(
            "media_path".into(),
            serde_json::Value::String("forged".into()),
        );
        assert!(
            serde_json::from_value::<CatalogAssetRecord>(serde_json::Value::Object(object))
                .is_err()
        );
    }
}
