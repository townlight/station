use station_reporting::runtime::{
    AsRunState, JournalTake, ProjectionInput, ReportingRuntime, render_csv, render_xmltv,
};

const CHANNEL: &str = "00000000-0000-0000-0000-000000000001";

fn take(sequence: u64, start: i64, end: i64, state: AsRunState) -> JournalTake {
    JournalTake {
        journal_id: "00000000-0000-0000-0000-000000000010".into(),
        journal_sequence: sequence,
        channel_id: CHANNEL.into(),
        started_at_unix_ms: start,
        ended_at_unix_ms: end,
        state,
        content_id: Some("00000000-0000-0000-0000-000000000020".into()),
        franchise_id: Some("00000000-0000-0000-0000-000000000030".into()),
        category: Some("program".into()),
        title: Some("Council & Budget".into()),
        catalog_revision: Some(7),
        schedule_revision: Some(9),
        journal_hash: "a".repeat(64),
        unavailable_reason: None,
    }
}

#[test]
fn journal_projection_is_idempotent_contiguous_and_marks_explicit_gap() {
    let mut runtime = ReportingRuntime::new();
    let first = take(1, 1_700_000_000_000, 1_700_000_060_000, AsRunState::Program);
    let mut gap = take(2, 1_700_000_060_000, 1_700_000_120_000, AsRunState::Gap);
    gap.content_id = None;
    gap.franchise_id = None;
    gap.category = None;
    gap.title = None;
    gap.catalog_revision = None;
    gap.schedule_revision = None;
    gap.unavailable_reason =
        Some("Journal recorded an explicit gap; no aired content is asserted.".into());
    let input = ProjectionInput::new(CHANNEL, "America/Denver", vec![first, gap]).unwrap();
    let once = runtime.project(input.clone()).unwrap();
    let twice = runtime.project(input).unwrap();
    assert_eq!(once.entries, twice.entries);
    assert_eq!(once.checkpoint.journal_sequence, 2);
    assert!(
        once.entries[1]
            .unavailable_reason
            .as_deref()
            .unwrap()
            .contains("gap")
    );
}

#[test]
fn projection_rejects_noncontiguous_journal_and_exports_escape_without_claiming_delivery() {
    let mut runtime = ReportingRuntime::new();
    let first = take(1, 1_700_000_000_000, 1_700_000_060_000, AsRunState::Program);
    let second = take(
        3,
        1_700_000_061_000,
        1_700_000_120_000,
        AsRunState::Fallback,
    );
    assert!(ProjectionInput::new(CHANNEL, "America/Denver", vec![first, second]).is_err());

    let projection = runtime
        .project(
            ProjectionInput::new(
                CHANNEL,
                "America/Denver",
                vec![take(
                    1,
                    1_700_000_000_000,
                    1_700_000_060_000,
                    AsRunState::Program,
                )],
            )
            .unwrap(),
        )
        .unwrap();
    let csv = render_csv(&projection).unwrap();
    let xmltv = render_xmltv(&projection, "TownLight <1>").unwrap();
    assert!(csv.contains("\"Council & Budget\""));
    assert!(csv.contains("2023-11-14T22:13:20.000Z"));
    assert!(xmltv.contains("TownLight &lt;1&gt;"));
    assert!(!csv.contains("delivered"));
}

#[test]
fn csv_neutralizes_spreadsheet_formula_prefixes() {
    let mut runtime = ReportingRuntime::new();
    let mut formula = take(1, 1_700_000_000_000, 1_700_000_060_000, AsRunState::Program);
    formula.title = Some("=SUM(1,1)".into());
    let projection = runtime
        .project(ProjectionInput::new(CHANNEL, "America/Denver", vec![formula]).unwrap())
        .unwrap();
    assert!(render_csv(&projection).unwrap().contains("\"'=SUM(1,1)\""));
}

#[test]
fn hostile_metadata_is_rejected_and_csv_quotes_every_field() {
    let mut non_program = take(1, 1_700_000_000_000, 1_700_000_060_000, AsRunState::Live);
    assert!(ProjectionInput::new(CHANNEL, "America/Denver", vec![non_program.clone()]).is_err());
    non_program.content_id = None;
    non_program.franchise_id = None;
    non_program.category = None;
    non_program.title = None;
    non_program.catalog_revision = None;
    non_program.schedule_revision = None;
    non_program.unavailable_reason = Some("Live authority unavailable.".into());
    assert!(ProjectionInput::new(CHANNEL, "America/Denver", vec![non_program]).is_ok());
    let mut control = take(1, 1_700_000_000_000, 1_700_000_060_000, AsRunState::Program);
    control.title = Some("bad\nvalue".into());
    assert!(ProjectionInput::new(CHANNEL, "America/Denver", vec![control]).is_err());
}
