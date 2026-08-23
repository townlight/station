use station_auth::{
    AccountId, BOOTSTRAP_MAX_LIFETIME_MS, BootstrapCredential, BootstrapInvalidReason,
    CredentialVersion, OperatorSession, RecoveryCredential, SESSION_ABSOLUTE_TIMEOUT_MS,
    SESSION_IDLE_TIMEOUT_MS, SecretDigest, SessionInvalidReason,
};

const ACCOUNT_ID: &str = "dc3c0d56-44c8-4c4f-bd32-b501c56d34f6";

fn digest(marker: u8) -> SecretDigest {
    SecretDigest::from_bytes([marker; 32])
}

fn session() -> OperatorSession {
    OperatorSession::new(
        digest(1),
        digest(2),
        AccountId::try_from(ACCOUNT_ID).unwrap(),
        CredentialVersion::new(3).unwrap(),
        digest(3),
        1_000,
        2_000,
        2_000 + SESSION_IDLE_TIMEOUT_MS,
        1_000 + SESSION_ABSOLUTE_TIMEOUT_MS,
    )
    .unwrap()
}

#[test]
fn bootstrap_credential_is_short_lived_single_use_authority() {
    let active =
        BootstrapCredential::new(digest(4), 1_000, 1_000 + BOOTSTRAP_MAX_LIFETIME_MS, None)
            .unwrap();
    assert_eq!(
        active.validate_at(999),
        Err(BootstrapInvalidReason::NotYetValid)
    );
    assert_eq!(active.validate_at(1_000), Ok(()));
    assert_eq!(
        active.validate_at(1_000 + BOOTSTRAP_MAX_LIFETIME_MS),
        Err(BootstrapInvalidReason::Expired)
    );

    let consumed = BootstrapCredential::new(digest(4), 1_000, 2_000, Some(1_500)).unwrap();
    assert_eq!(
        consumed.validate_at(1_600),
        Err(BootstrapInvalidReason::Consumed)
    );
}

#[test]
fn bootstrap_and_recovery_records_reject_impossible_times() {
    assert!(BootstrapCredential::new(digest(4), -1, 2_000, None).is_err());
    assert!(BootstrapCredential::new(digest(4), 1_000, 1_000, None).is_err());
    assert!(
        BootstrapCredential::new(digest(4), 1_000, 1_001 + BOOTSTRAP_MAX_LIFETIME_MS, None,)
            .is_err()
    );
    assert!(BootstrapCredential::new(digest(4), 1_000, 2_000, Some(999)).is_err());

    let account = AccountId::try_from(ACCOUNT_ID).unwrap();
    assert!(RecoveryCredential::new(digest(5), account.clone(), -1, None).is_err());
    assert!(RecoveryCredential::new(digest(5), account, 1_000, Some(999)).is_err());
}

#[test]
fn session_constructor_enforces_distinct_secrets_and_bounded_monotonic_times() {
    let account = || AccountId::try_from(ACCOUNT_ID).unwrap();
    let version = CredentialVersion::new(3).unwrap();
    assert!(
        OperatorSession::new(
            digest(1),
            digest(1),
            account(),
            version,
            digest(3),
            1_000,
            1_000,
            1_000 + SESSION_IDLE_TIMEOUT_MS,
            1_000 + SESSION_ABSOLUTE_TIMEOUT_MS,
        )
        .is_err()
    );
    assert!(
        OperatorSession::new(
            digest(1),
            digest(2),
            account(),
            version,
            digest(3),
            1_000,
            999,
            2_000,
            3_000,
        )
        .is_err()
    );
    assert!(
        OperatorSession::new(
            digest(1),
            digest(2),
            account(),
            version,
            digest(3),
            1_000,
            2_000,
            2_001 + SESSION_IDLE_TIMEOUT_MS,
            1_000 + SESSION_ABSOLUTE_TIMEOUT_MS,
        )
        .is_err()
    );
    assert!(
        OperatorSession::new(
            digest(1),
            digest(2),
            account(),
            version,
            digest(3),
            1_000,
            2_000,
            3_000,
            1_001 + SESSION_ABSOLUTE_TIMEOUT_MS,
        )
        .is_err()
    );
}

#[test]
fn session_validation_binds_account_credential_boot_and_expiry() {
    let session = session();
    let version = CredentialVersion::new(3).unwrap();
    assert_eq!(
        session.validate_at(2_000, &digest(3), version, true),
        Ok(())
    );
    assert_eq!(
        session.validate_at(2_000, &digest(3), version, false),
        Err(SessionInvalidReason::AccountDisabled)
    );
    assert_eq!(
        session.validate_at(2_000, &digest(3), CredentialVersion::new(4).unwrap(), true),
        Err(SessionInvalidReason::CredentialChanged)
    );
    assert_eq!(
        session.validate_at(2_000, &digest(9), version, true),
        Err(SessionInvalidReason::DifferentServiceBoot)
    );
    assert_eq!(
        session.validate_at(999, &digest(3), version, true),
        Err(SessionInvalidReason::NotYetValid)
    );
    assert_eq!(
        session.validate_at(session.idle_expires_at_unix_ms(), &digest(3), version, true),
        Err(SessionInvalidReason::IdleExpired)
    );
    assert_eq!(
        session.validate_at(
            session.absolute_expires_at_unix_ms(),
            &digest(3),
            version,
            true
        ),
        Err(SessionInvalidReason::AbsoluteExpired)
    );
}
