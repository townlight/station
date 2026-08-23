use station_auth::{
    OperatorPassword, PasswordPhc, hash_password, verify_password, verify_password_or_dummy,
};

#[test]
fn password_hash_uses_exact_argon2id_contract_and_random_salt() {
    let password = OperatorPassword::new("correct horse battery staple").unwrap();
    let first = hash_password(&password).unwrap();
    let second = hash_password(&password).unwrap();

    assert!(
        first
            .as_str()
            .starts_with("$argon2id$v=19$m=65536,t=3,p=1$")
    );
    assert_ne!(first.as_str(), second.as_str());
    let encoded_hash = first.as_str().rsplit('$').next().unwrap();
    assert_eq!(
        encoded_hash.len(),
        43,
        "32 Argon2 output bytes encode to 43 PHC base64 characters"
    );
}

#[test]
fn password_verification_accepts_only_the_matching_password() {
    let correct = OperatorPassword::new("correct horse battery staple").unwrap();
    let wrong = OperatorPassword::new("this password is definitely wrong").unwrap();
    let phc = hash_password(&correct).unwrap();

    assert!(verify_password(&correct, &phc).unwrap());
    assert!(!verify_password(&wrong, &phc).unwrap());
}

#[test]
fn password_phc_rejects_algorithm_or_parameter_drift() {
    for invalid in [
        "$argon2i$v=19$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=16$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=19$m=32768,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=19$m=65536,t=2,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=19$m=65536,t=3,p=2$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=19$m=65536,t=3,p=1,x=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=19$m=65536,t=3,p=1$YWJj$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
        "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFB",
    ] {
        assert!(
            PasswordPhc::try_from(invalid).is_err(),
            "accepted {invalid}"
        );
    }
}

#[test]
fn absent_account_still_runs_the_dummy_verification_path() {
    let password = OperatorPassword::new("correct horse battery staple").unwrap();
    let phc = hash_password(&password).unwrap();

    assert!(verify_password_or_dummy(&password, Some(&phc)).unwrap());
    assert!(!verify_password_or_dummy(&password, None).unwrap());
}
