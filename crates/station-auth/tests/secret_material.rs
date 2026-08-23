use std::collections::BTreeSet;

use station_auth::{
    RECOVERY_CODE_COUNT, RecoveryCode, SecretToken, digest_recovery_code, digest_secret,
    generate_recovery_codes, generate_secret_token,
};

#[test]
fn generated_tokens_have_256_bits_and_digest_without_secret_storage() {
    let first = generate_secret_token().unwrap();
    let second = generate_secret_token().unwrap();

    assert_eq!(first.expose_secret().len(), 43);
    assert_ne!(first.expose_secret(), second.expose_secret());
    assert_eq!(digest_secret(&first).as_bytes().len(), 32);
    assert!(!format!("{first:?}").contains(first.expose_secret()));
    assert!(!format!("{:?}", digest_secret(&first)).contains(first.expose_secret()));
}

#[test]
fn persisted_token_parser_accepts_only_exact_256_bit_base64url() {
    let generated = generate_secret_token().unwrap();
    assert!(SecretToken::try_from(generated.expose_secret()).is_ok());
    for invalid in [
        "short",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA+",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    ] {
        assert!(
            SecretToken::try_from(invalid).is_err(),
            "accepted {invalid}"
        );
    }
}

#[test]
fn recovery_bundle_contains_eight_unique_hundred_bit_grouped_codes() {
    let bundle = generate_recovery_codes().unwrap();
    assert_eq!(bundle.display_codes.len(), RECOVERY_CODE_COUNT);
    assert_eq!(bundle.storage_digests.len(), RECOVERY_CODE_COUNT);

    let codes = bundle
        .display_codes
        .iter()
        .map(|code| code.expose_secret())
        .collect::<BTreeSet<_>>();
    assert_eq!(codes.len(), RECOVERY_CODE_COUNT);
    for code in &bundle.display_codes {
        let parts = code.expose_secret().split('-').collect::<Vec<_>>();
        assert_eq!(parts.len(), 5);
        assert_eq!(parts[0], "TL");
        assert!(parts[1..].iter().all(|part| {
            part.len() == 5
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_uppercase() || (b'2'..=b'7').contains(&byte))
        }));
        assert!(!format!("{code:?}").contains(code.expose_secret()));
    }
    for (code, digest) in bundle.display_codes.iter().zip(bundle.storage_digests) {
        assert_eq!(digest_recovery_code(code), digest);
        assert!(
            !digest
                .as_bytes()
                .windows(code.expose_secret().len())
                .any(|value| { value == code.expose_secret().as_bytes() })
        );
    }
}

#[test]
fn recovery_code_parser_is_strict() {
    assert!(RecoveryCode::try_from("TL-ABCDE-FGHIJ-KLMNO-PQRST").is_ok());
    for invalid in [
        "tl-ABCDE-FGHIJ-KLMNO-PQRST",
        "TL-ABCDE-FGHIJ-KLMNO-PQRS",
        "TL-ABCDE-FGHIJ-KLMNO-PQRS1",
        "TL-ABCDE-FGHIJ-KLMNO-PQRS8",
        " TL-ABCDE-FGHIJ-KLMNO-PQRST",
    ] {
        assert!(
            RecoveryCode::try_from(invalid).is_err(),
            "accepted {invalid}"
        );
    }
}
