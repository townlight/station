use station_contribution::{
    Actor, ConnectionReceipt, ContributionRole, ContributionRoom, ContributionSession, Invitation,
    InvitationState, ReceiptKind, RoomState, SessionState, TurnIntent, VdoNinjaBridgeIntent,
    apply_connection_receipt, claim_invitation, create_session, expire_invitation,
    issue_invitation, open_room, revoke_invitation,
};

fn identity(suffix: &str) -> String {
    format!("00000000-0000-4000-8000-0000000000{suffix}")
}

fn actor(suffix: &str, role: ContributionRole) -> Actor {
    Actor {
        actor_id: identity(suffix),
        role,
    }
}

fn bridge() -> VdoNinjaBridgeIntent {
    VdoNinjaBridgeIntent {
        configuration_id: identity("04"),
        signalling_origin: "https://vdo.example.test:443".into(),
        turn: Some(TurnIntent {
            relay_uri: "turns://turn.example.test:5349".into(),
            credential_reference_id: identity("05"),
        }),
    }
}

fn room() -> ContributionRoom {
    ContributionRoom {
        room_id: identity("01"),
        channel_id: identity("02"),
        created_by_operator: identity("03"),
        bridge: bridge(),
        state: RoomState::Draft,
        revision: 1,
    }
}

fn invitation() -> Invitation {
    Invitation {
        invitation_id: identity("06"),
        room_id: identity("01"),
        contributor_id: identity("07"),
        issued_by_operator: identity("03"),
        issued_at_utc_ms: 1_000,
        expires_at_utc_ms: 3_600_000,
        state: InvitationState::Issued,
        revision: 1,
    }
}

fn setup_session() -> (
    ContributionRoom,
    Invitation,
    ContributionSession,
    Actor,
    Actor,
) {
    let operator = actor("03", ContributionRole::Operator);
    let contributor = actor("07", ContributionRole::Contributor);
    let worker = actor("08", ContributionRole::Worker);
    let room = open_room(&room(), 1, &operator).unwrap();
    let invitation = issue_invitation(&room, &operator, invitation()).unwrap();
    let invitation =
        claim_invitation(&invitation, 1, &contributor, &identity("09"), 2_000).unwrap();
    let session = create_session(
        &room,
        &invitation,
        &operator,
        &worker,
        identity("09"),
        2_000,
    )
    .unwrap();
    (room, invitation, session, worker, contributor)
}

fn receipt(session: &ContributionSession, sequence: u64, kind: ReceiptKind) -> ConnectionReceipt {
    ConnectionReceipt {
        receipt_id: identity(&format!("1{sequence}")),
        room_id: session.room_id.clone(),
        session_id: session.session_id.clone(),
        invitation_id: session.invitation_id.clone(),
        worker_id: session.worker_id.clone(),
        sequence,
        kind,
        observed_at_utc_ms: 3_000 + sequence as i64,
    }
}

#[test]
fn invitation_is_operator_issued_contributor_bound_single_use_and_expires() {
    let operator = actor("03", ContributionRole::Operator);
    let contributor = actor("07", ContributionRole::Contributor);
    let room = open_room(&room(), 1, &operator).unwrap();
    let issued = issue_invitation(&room, &operator, invitation()).unwrap();
    let claimed = claim_invitation(&issued, 1, &contributor, &identity("09"), 2_000).unwrap();
    assert!(matches!(&claimed.state, InvitationState::Claimed { .. }));
    assert!(claim_invitation(&claimed, 2, &contributor, &identity("10"), 2_001).is_err());

    let expired = expire_invitation(&issued, 1, 3_600_000).unwrap();
    assert!(matches!(&expired.state, InvitationState::Expired { .. }));
    assert!(claim_invitation(&expired, 2, &contributor, &identity("11"), 3_600_001).is_err());
    assert!(revoke_invitation(&claimed, 2, &operator, 2_001).is_err());
}

#[test]
fn contribution_bridge_intent_is_bounded_nonsecret_and_room_identity_cannot_rebind() {
    let operator = actor("03", ContributionRole::Operator);
    let draft = room();
    assert!(draft.validate_create().is_ok());
    let opened = open_room(&draft, 1, &operator).unwrap();
    assert!(open_room(&opened, 1, &operator).is_err());

    let mut unsafe_bridge = draft;
    unsafe_bridge.bridge.signalling_origin = "https://vdo.example.test/?secret=not-allowed".into();
    assert!(unsafe_bridge.validate_create().is_err());

    let mut rebound = opened;
    rebound.channel_id = identity("12");
    assert!(rebound.validate_successor_of(&room(), 1).is_err());
}

#[test]
fn worker_receipts_are_append_only_and_recovery_is_terminal_fail_closed() {
    let (room, invitation, session, worker, _) = setup_session();
    let connecting = receipt(&session, 1, ReceiptKind::Connecting);
    let connecting_session = apply_connection_receipt(
        &room,
        &invitation,
        &session,
        1,
        &worker,
        &[],
        connecting.clone(),
    )
    .unwrap();
    assert_eq!(connecting_session.state, SessionState::Connecting);

    assert!(
        apply_connection_receipt(
            &room,
            &invitation,
            &connecting_session,
            2,
            &worker,
            std::slice::from_ref(&connecting),
            connecting.clone(),
        )
        .is_err()
    );

    let connected = receipt(&connecting_session, 2, ReceiptKind::Connected);
    let connected_session = apply_connection_receipt(
        &room,
        &invitation,
        &connecting_session,
        2,
        &worker,
        &[receipt(&session, 1, ReceiptKind::Connecting)],
        connected.clone(),
    )
    .unwrap();
    let recovered = receipt(&connected_session, 3, ReceiptKind::RecoveryStopped);
    let failed = apply_connection_receipt(
        &room,
        &invitation,
        &connected_session,
        3,
        &worker,
        &[receipt(&session, 1, ReceiptKind::Connecting), connected],
        recovered,
    )
    .unwrap();
    assert_eq!(failed.state, SessionState::Failed);
    assert!(
        apply_connection_receipt(
            &room,
            &invitation,
            &failed,
            4,
            &worker,
            &[],
            receipt(&failed, 4, ReceiptKind::Connected),
        )
        .is_err()
    );
}

#[test]
fn stale_revisions_and_role_or_authority_mismatch_fail_closed() {
    let (room, invitation, session, worker, contributor) = setup_session();
    assert!(open_room(&room, 1, &actor("03", ContributionRole::Operator)).is_err());
    assert!(
        apply_connection_receipt(
            &room,
            &invitation,
            &session,
            0,
            &worker,
            &[],
            receipt(&session, 1, ReceiptKind::Connecting),
        )
        .is_err()
    );
    assert!(
        apply_connection_receipt(
            &room,
            &invitation,
            &session,
            1,
            &contributor,
            &[],
            receipt(&session, 1, ReceiptKind::Connecting),
        )
        .is_err()
    );
}
