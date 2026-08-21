use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::time::Duration;

use station_alerting::{AlertTransport, DeliveryEnvelope, WebhookTransport, sign_webhook_payload};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertDelivery, AlertDeliveryPayload,
    AlertDeliveryState, AlertNotificationKind, AlertSeverity, redact_alert_channel_target,
};
use station_storage::AlertChannelSecret;

fn envelope(target: String) -> DeliveryEnvelope {
    DeliveryEnvelope {
        delivery: AlertDelivery {
            delivery_id: "delivery-webhook".into(),
            event_id: "event-webhook".into(),
            alert_channel_id: "7be5438c-0ecf-45a5-b807-f39179857b17".into(),
            notification_kind: AlertNotificationKind::Firing,
            state: AlertDeliveryState::Leased,
            attempts: 1,
            next_attempt_at_unix_ms: 1_000,
            lease_expires_at_unix_ms: Some(31_000),
            last_error: String::new(),
            signature: None,
            created_at_unix_ms: 1_000,
            updated_at_unix_ms: 1_000,
            sent_at_unix_ms: None,
        },
        channel: AlertChannel {
            channel_id: "7be5438c-0ecf-45a5-b807-f39179857b17".into(),
            kind: AlertChannelKind::Webhook,
            label: "Loopback proof".into(),
            enabled: true,
            target_redacted: redact_alert_channel_target(AlertChannelKind::Webhook, &target)
                .unwrap(),
            credential_configured: true,
            quiet_hours_start_utc: None,
            quiet_hours_end_utc: None,
            created_at_unix_ms: 100,
            updated_at_unix_ms: 100,
        },
        secret: AlertChannelSecret {
            target,
            endpoint: None,
            sender: None,
            identity: None,
            credential: "webhook-signing-secret".into(),
        },
        payload: AlertDeliveryPayload {
            notification_kind: AlertNotificationKind::Firing,
            event_id: "event-webhook".into(),
            condition: AlertConditionKind::OffAir,
            severity: AlertSeverity::Critical,
            resource_ref: "government-channel".into(),
            summary: "Government channel is off air.".into(),
            detail: "No verified output is reaching the sink.".into(),
            source_section: "S8".into(),
            first_observed_at_unix_ms: 1_000,
            last_observed_at_unix_ms: 1_000,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
        },
    }
}

#[test]
fn hmac_sha256_matches_the_rfc_4231_vector() {
    let key = vec![0x0b; 20];
    assert_eq!(
        sign_webhook_payload(&key, b"Hi There"),
        "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    );
}

#[test]
fn posts_a_signed_redaction_safe_payload_to_a_real_loopback_receiver() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = mpsc::sync_channel(1);
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut request = Vec::new();
        let mut chunk = [0_u8; 4096];
        loop {
            let count = stream.read(&mut chunk).unwrap();
            if count == 0 {
                break;
            }
            request.extend_from_slice(&chunk[..count]);
            if let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&request[..header_end + 4]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("content-length: ")
                            .or_else(|| line.strip_prefix("Content-Length: "))
                    })
                    .unwrap()
                    .parse::<usize>()
                    .unwrap();
                if request.len() >= header_end + 4 + content_length {
                    break;
                }
            }
        }
        stream
            .write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .unwrap();
        sender.send(request).unwrap();
    });

    let target = format!("http://{address}/operator-alert");
    let delivery = envelope(target);
    let receipt = WebhookTransport::new(Duration::from_secs(5))
        .deliver(&delivery)
        .unwrap();
    server.join().unwrap();
    let request = String::from_utf8(receiver.recv().unwrap()).unwrap();
    assert!(request.starts_with("POST /operator-alert HTTP/1.1\r\n"));
    assert!(
        request
            .to_ascii_lowercase()
            .contains("content-type: application/json")
    );
    let (headers, body) = request.split_once("\r\n\r\n").unwrap();
    let signature = headers
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .starts_with("x-townlight-signature: ")
                .then(|| line.split_once(": ").unwrap().1)
        })
        .unwrap();
    assert_eq!(
        signature,
        sign_webhook_payload(b"webhook-signing-secret", body.as_bytes())
    );
    assert_eq!(receipt.signature.as_deref(), Some(signature));
    assert!(body.contains("\"schema_version\":1"));
    assert!(body.contains("\"notification_kind\":\"firing\""));
    assert!(body.contains("\"condition\":\"off-air\""));
    assert!(!body.contains("webhook-signing-secret"));
    assert!(!body.contains("127.0.0.1"));
}

#[test]
fn refuses_provider_redirects_without_forwarding_the_signed_payload() {
    let redirect_target = TcpListener::bind("127.0.0.1:0").unwrap();
    redirect_target.set_nonblocking(true).unwrap();
    let redirect_address = redirect_target.local_addr().unwrap();
    let origin = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin_address = origin.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = origin.accept().unwrap();
        let mut request = [0_u8; 4096];
        let _ = stream.read(&mut request).unwrap();
        write!(
            stream,
            "HTTP/1.1 307 Temporary Redirect\r\nLocation: http://{redirect_address}/stolen\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        .unwrap();
    });
    let result = WebhookTransport::new(Duration::from_secs(5))
        .deliver(&envelope(format!("http://{origin_address}/operator-alert")));
    server.join().unwrap();
    assert_eq!(result.unwrap_err(), "webhook provider returned HTTP 307");
    std::thread::sleep(Duration::from_millis(50));
    assert!(redirect_target.accept().is_err());
}
