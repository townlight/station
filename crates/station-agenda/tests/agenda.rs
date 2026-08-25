use station_agenda::{
    Agenda, AgendaActor, AgendaChapter, AgendaItem, AgendaItemVisibility, AgendaProvenance,
    AgendaSyncIntent, validate_agenda_write,
};

const MEETING_START: i64 = 10_000;

fn identity(suffix: &str) -> String {
    format!("00000000-0000-4000-8000-0000000000{suffix}")
}

fn staff() -> AgendaActor {
    AgendaActor {
        actor_id: identity("01"),
    }
}

fn agenda() -> Agenda {
    Agenda {
        agenda_id: identity("02"),
        channel_id: identity("03"),
        meeting_id: identity("04"),
        title: "Town council regular meeting".into(),
        meeting_starts_at_unix_ms: MEETING_START,
        provenance: AgendaProvenance::StaffAuthored {
            author: staff(),
            asserted_at_unix_ms: 1_000,
        },
        sync_intent: AgendaSyncIntent {
            channel_id: identity("03"),
            schedule_item_id: identity("05"),
            schedule_revision: 7,
            media_asset_sha256: "a".repeat(64),
            expected_media_duration_ms: 60_000,
            requested_by: staff(),
            requested_at_unix_ms: 2_000,
        },
        items: vec![
            AgendaItem {
                item_id: identity("06"),
                ordinal: 1,
                title: "Call to order".into(),
                public_summary: Some("Opening business".into()),
                staff_notes: Some("Confirm quorum before motion".into()),
                visibility: AgendaItemVisibility::Public,
            },
            AgendaItem {
                item_id: identity("07"),
                ordinal: 2,
                title: "Personnel discussion".into(),
                public_summary: None,
                staff_notes: Some("Executive-session material".into()),
                visibility: AgendaItemVisibility::StaffOnly,
            },
        ],
        chapters: vec![
            AgendaChapter {
                chapter_id: identity("08"),
                agenda_item_id: identity("06"),
                ordinal: 1,
                title: "Opening".into(),
                starts_at_media_offset_ms: 0,
                ends_at_media_offset_ms: Some(20_000),
            },
            AgendaChapter {
                chapter_id: identity("09"),
                agenda_item_id: identity("07"),
                ordinal: 2,
                title: "Closed session".into(),
                starts_at_media_offset_ms: 20_000,
                ends_at_media_offset_ms: Some(60_000),
            },
        ],
        revision: 1,
    }
}

#[test]
fn public_projection_is_deterministic_and_excludes_staff_only_content() {
    let agenda = agenda();
    let projection = agenda.public_projection().unwrap();
    assert_eq!(projection.agenda_id, agenda.agenda_id);
    assert_eq!(projection.items.len(), 1);
    assert_eq!(projection.items[0].ordinal, 1);
    assert_eq!(projection.items[0].title, "Call to order");
    assert_eq!(projection.items[0].chapters.len(), 1);
    assert_eq!(projection.items[0].chapters[0].title, "Opening");
    assert!(!format!("{projection:?}").contains("quorum"));
    assert!(!format!("{projection:?}").contains("Closed session"));
}

#[test]
fn agenda_writes_use_exact_cas_without_identity_or_provenance_rebinding() {
    let first = agenda();
    validate_agenda_write(None, None, &first).unwrap();
    assert!(validate_agenda_write(Some(1), None, &first).is_err());

    let mut second = first.clone();
    second.revision = 2;
    second.title = "Town council regular meeting (revised)".into();
    second.sync_intent.schedule_revision = 8;
    validate_agenda_write(Some(1), Some(&first), &second).unwrap();
    assert!(validate_agenda_write(Some(0), Some(&first), &second).is_err());

    let mut rebound_meeting = second.clone();
    rebound_meeting.meeting_id = identity("10");
    assert!(
        validate_agenda_write(Some(1), Some(&first), &rebound_meeting)
            .unwrap_err()
            .contains("identity")
    );

    let mut rebound_provenance = second;
    rebound_provenance.provenance = AgendaProvenance::PdfImportClaim {
        importer: staff(),
        source_sha256: "b".repeat(64),
        asserted_at_unix_ms: 3_000,
    };
    assert!(
        validate_agenda_write(Some(1), Some(&first), &rebound_provenance)
            .unwrap_err()
            .contains("provenance")
    );
}

#[test]
fn declared_pdf_import_is_provenance_not_an_execution_or_publication_claim() {
    let mut imported = agenda();
    imported.provenance = AgendaProvenance::PdfImportClaim {
        importer: staff(),
        source_sha256: "b".repeat(64),
        asserted_at_unix_ms: 3_000,
    };
    imported.items[0].staff_notes = None;
    imported.validate().unwrap();
    let projection = imported.public_projection().unwrap();
    assert_eq!(projection.items.len(), 1);
    assert!(!format!("{projection:?}").contains("PdfImport"));
}

#[test]
fn malformed_ordering_unbounded_text_and_invalid_sync_intent_fail_closed() {
    let mut invalid = agenda();
    invalid.items[1].ordinal = 3;
    assert!(invalid.validate().unwrap_err().contains("item order"));

    let mut invalid = agenda();
    invalid.chapters[1].starts_at_media_offset_ms = 10_000;
    assert!(invalid.validate().unwrap_err().contains("chapter order"));

    let mut invalid = agenda();
    invalid.sync_intent.channel_id = identity("10");
    assert!(invalid.validate().unwrap_err().contains("channel"));

    let mut invalid = agenda();
    invalid.items[0].title = "x".repeat(201);
    assert!(
        invalid
            .validate()
            .unwrap_err()
            .contains("agenda item title")
    );
}
