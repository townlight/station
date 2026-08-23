use std::collections::BTreeSet;

use station_auth::{
    AccountId, CredentialVersion, OperatorAccount, OperatorDisplayName, OperatorPassword,
    OperatorRole, OperatorUsername, PasswordPhc,
};

const ACCOUNT_ID: &str = "dc3c0d56-44c8-4c4f-bd32-b501c56d34f6";
const VALID_PHC: &str = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE";

#[test]
fn identity_values_reject_noncanonical_or_unbounded_input() {
    assert!(AccountId::try_from(ACCOUNT_ID).is_ok());
    assert!(AccountId::try_from("DC3C0D56-44C8-4C4F-BD32-B501C56D34F6").is_err());
    assert!(AccountId::try_from("not-a-uuid").is_err());

    assert_eq!(
        OperatorUsername::try_from("control.room-1")
            .unwrap()
            .as_str(),
        "control.room-1"
    );
    for invalid in ["ab", "UPPER", "has space", "a/b", "a".repeat(65).as_str()] {
        assert!(
            OperatorUsername::try_from(invalid).is_err(),
            "accepted {invalid:?}"
        );
    }

    assert!(OperatorDisplayName::try_from("Scott Converse").is_ok());
    assert!(OperatorDisplayName::try_from(" ").is_err());
    assert!(OperatorDisplayName::try_from("line\nbreak").is_err());
    assert!(OperatorDisplayName::try_from("x".repeat(129)).is_err());
}

#[test]
fn password_and_password_phc_debug_output_is_redacted() {
    let password = OperatorPassword::new("correct horse battery staple").unwrap();
    assert!(!format!("{password:?}").contains("correct horse"));

    let phc = PasswordPhc::try_from(VALID_PHC).unwrap();
    assert!(!format!("{phc:?}").contains("argon2id"));
}

#[test]
fn passwords_are_bounded_without_composition_rules() {
    assert!(OperatorPassword::new("short").is_err());
    assert!(OperatorPassword::new("a valid phrase with spaces").is_ok());
    assert!(OperatorPassword::new("x".repeat(129)).is_err());
    assert!(OperatorPassword::new("valid length but\0control").is_err());
}

#[test]
fn role_vocabulary_is_exact_and_round_trips() {
    let expected = [
        (OperatorRole::SetupAdmin, "setup_admin"),
        (OperatorRole::MeetingOperator, "meeting_operator"),
        (OperatorRole::RecordsClerk, "records_clerk"),
        (OperatorRole::PublishOperator, "publish_operator"),
        (OperatorRole::SupportAdmin, "support_admin"),
    ];
    for (role, text) in expected {
        assert_eq!(role.as_str(), text);
        assert_eq!(OperatorRole::try_from(text), Ok(role));
    }
    assert!(OperatorRole::try_from("admin").is_err());
    assert!(OperatorRole::try_from("viewer").is_err());
}

#[test]
fn account_requires_role_time_and_nonzero_credential_authority() {
    assert!(CredentialVersion::new(0).is_err());
    let version = CredentialVersion::new(1).unwrap();
    assert_eq!(version.next().unwrap().get(), 2);

    let values = || {
        (
            AccountId::try_from(ACCOUNT_ID).unwrap(),
            OperatorUsername::try_from("operator.one").unwrap(),
            OperatorDisplayName::try_from("Operator One").unwrap(),
            PasswordPhc::try_from(VALID_PHC).unwrap(),
        )
    };
    let (id, username, display, phc) = values();
    assert!(
        OperatorAccount::new(
            id,
            username,
            display,
            phc,
            BTreeSet::new(),
            version,
            true,
            1_000,
            1_000,
        )
        .is_err()
    );

    let (id, username, display, phc) = values();
    assert!(
        OperatorAccount::new(
            id,
            username,
            display,
            phc,
            BTreeSet::from([OperatorRole::SetupAdmin]),
            version,
            true,
            2_000,
            1_000,
        )
        .is_err()
    );
}
