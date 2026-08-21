use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::time::Duration;

use station_alerting::{AlertTransport, DeliveryEnvelope, SmsTransport, build_sms_message};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertDelivery, AlertDeliveryPayload,
    AlertDeliveryState, AlertNotificationKind, AlertSeverity, redact_alert_channel_target,
};
use station_storage::AlertChannelSecret;

fn envelope(endpoint: String) -> DeliveryEnvelope {
    DeliveryEnvelope {
        delivery: AlertDelivery {
            delivery_id: "delivery-sms".into(),
            event_id: "event-sms".into(),
            alert_channel_id: "31861f31-a066-473e-9328-c7adcc53b52c".into(),
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
            channel_id: "31861f31-a066-473e-9328-c7adcc53b52c".into(),
            kind: AlertChannelKind::Sms,
            label: "On-call phone".into(),
            enabled: true,
            target_redacted: redact_alert_channel_target(AlertChannelKind::Sms, "+15551234567")
                .unwrap(),
            credential_configured: true,
            quiet_hours_start_utc: None,
            quiet_hours_end_utc: None,
            created_at_unix_ms: 100,
            updated_at_unix_ms: 100,
        },
        secret: AlertChannelSecret {
            target: "+15551234567".into(),
            endpoint: Some(endpoint),
            sender: Some("+15557654321".into()),
            identity: Some("provider-account".into()),
            credential: "provider-token".into(),
        },
        payload: AlertDeliveryPayload {
            notification_kind: AlertNotificationKind::Firing,
            event_id: "event-sms".into(),
            condition: AlertConditionKind::EncoderDeath,
            severity: AlertSeverity::Critical,
            resource_ref: "government-channel".into(),
            summary: "Government channel encoder stopped.".into(),
            detail: "The supervisor is attempting recovery.".into(),
            source_section: "S8".into(),
            first_observed_at_unix_ms: 1_000,
            last_observed_at_unix_ms: 1_000,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
        },
    }
}

#[test]
fn keeps_sms_messages_within_the_contract_limit() {
    let mut delivery = envelope("http://127.0.0.1:1/messages".into());
    delivery.payload.summary = "A serious broadcast problem with unicode ⚠ ".repeat(10);
    let message = build_sms_message(&delivery.payload);
    assert!(message.chars().count() <= 140);
    assert!(message.starts_with("TownLight CRITICAL:"));
}

#[test]
fn posts_a_real_twilio_shaped_request_to_loopback() {
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
            request.extend_from_slice(&chunk[..count]);
            if let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&request[..header_end + 4]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length: ")
                            .map(str::to_owned)
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
            .write_all(b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .unwrap();
        sender.send(request).unwrap();
    });
    let delivery = envelope(format!("http://{address}/v1/messages"));
    let receipt = SmsTransport::new(Duration::from_secs(5))
        .deliver(&delivery)
        .unwrap();
    assert_eq!(receipt.signature, None);
    server.join().unwrap();
    let request = String::from_utf8(receiver.recv().unwrap()).unwrap();
    assert!(request.starts_with("POST /v1/messages HTTP/1.1\r\n"));
    assert!(
        request.to_ascii_lowercase().contains(
            "authorization: basic cHJvdmlkZXItYWNjb3VudDpwcm92aWRlci10b2tlbg=="
                .to_ascii_lowercase()
                .as_str()
        )
    );
    assert!(
        request
            .to_ascii_lowercase()
            .contains("content-type: application/x-www-form-urlencoded")
    );
    let body = request.split_once("\r\n\r\n").unwrap().1;
    assert!(body.contains("To=%2B15551234567"));
    assert!(body.contains("From=%2B15557654321"));
    assert!(body.contains("Body=TownLight"));
    assert!(!body.contains("provider-token"));
}
