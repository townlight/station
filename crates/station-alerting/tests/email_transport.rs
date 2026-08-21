use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc;
use std::time::Duration;

use station_alerting::{AlertTransport, DeliveryEnvelope, EmailTransport};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertDelivery, AlertDeliveryPayload,
    AlertDeliveryState, AlertNotificationKind, AlertSeverity, redact_alert_channel_target,
};
use station_storage::AlertChannelSecret;

fn envelope(endpoint: String) -> DeliveryEnvelope {
    DeliveryEnvelope {
        delivery: AlertDelivery {
            delivery_id: "delivery-email".into(),
            event_id: "event-email".into(),
            alert_channel_id: "046bf065-8015-4f11-8fe2-a03d2052011c".into(),
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
            channel_id: "046bf065-8015-4f11-8fe2-a03d2052011c".into(),
            kind: AlertChannelKind::Email,
            label: "Operations email".into(),
            enabled: true,
            target_redacted: redact_alert_channel_target(
                AlertChannelKind::Email,
                "operator@example.org",
            )
            .unwrap(),
            credential_configured: true,
            quiet_hours_start_utc: None,
            quiet_hours_end_utc: None,
            created_at_unix_ms: 100,
            updated_at_unix_ms: 100,
        },
        secret: AlertChannelSecret {
            target: "operator@example.org".into(),
            endpoint: Some(endpoint),
            sender: Some("station@example.org".into()),
            identity: Some("smtp-account".into()),
            credential: "smtp-password".into(),
        },
        payload: AlertDeliveryPayload {
            notification_kind: AlertNotificationKind::Firing,
            event_id: "event-email".into(),
            condition: AlertConditionKind::OffAir,
            severity: AlertSeverity::Critical,
            resource_ref: "government-channel".into(),
            summary: "Government channel is off air.".into(),
            detail: "No verified output is reaching the configured sink.".into(),
            source_section: "S8".into(),
            first_observed_at_unix_ms: 1_000,
            last_observed_at_unix_ms: 1_000,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
        },
    }
}

fn read_line(reader: &mut BufReader<TcpStream>, transcript: &mut String) -> String {
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    transcript.push_str(&line);
    line
}

#[test]
fn sends_a_real_authenticated_message_to_a_loopback_smtp_relay() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = mpsc::sync_channel(1);
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut transcript = String::new();
        stream.write_all(b"220 localhost ESMTP\r\n").unwrap();
        assert!(read_line(&mut reader, &mut transcript).starts_with("EHLO "));
        stream
            .write_all(b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            .unwrap();
        assert!(read_line(&mut reader, &mut transcript).starts_with("AUTH "));
        stream.write_all(b"235 2.7.0 authenticated\r\n").unwrap();
        assert!(read_line(&mut reader, &mut transcript).starts_with("MAIL FROM:"));
        stream.write_all(b"250 2.1.0 ok\r\n").unwrap();
        assert!(read_line(&mut reader, &mut transcript).starts_with("RCPT TO:"));
        stream.write_all(b"250 2.1.5 ok\r\n").unwrap();
        assert_eq!(read_line(&mut reader, &mut transcript), "DATA\r\n");
        stream
            .write_all(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            .unwrap();
        loop {
            if read_line(&mut reader, &mut transcript) == ".\r\n" {
                break;
            }
        }
        stream.write_all(b"250 2.0.0 queued\r\n").unwrap();
        sender.send(transcript).unwrap();
    });
    let delivery = envelope(format!("smtp://{address}"));
    let receipt = EmailTransport::new(Duration::from_secs(5))
        .deliver(&delivery)
        .unwrap();
    assert_eq!(receipt.signature, None);
    server.join().unwrap();
    let transcript = receiver.recv().unwrap();
    assert!(transcript.contains("MAIL FROM:<station@example.org>"));
    assert!(transcript.contains("RCPT TO:<operator@example.org>"));
    assert!(transcript.contains("Subject: [TownLight CRITICAL] off-air"));
    assert!(transcript.contains("Government channel is off air."));
    assert!(!transcript.contains("smtp-password"));
}

#[test]
fn rejects_header_control_characters_before_connecting() {
    let mut delivery = envelope("smtp://127.0.0.1:1".into());
    delivery.payload.resource_ref = "channel\r\nBcc: attacker@example.org".into();
    assert!(
        EmailTransport::new(Duration::from_millis(100))
            .deliver(&delivery)
            .unwrap_err()
            .contains("invalid")
    );
}
