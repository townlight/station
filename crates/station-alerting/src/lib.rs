//! Durable operator alert delivery.

use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use hmac::{Hmac, Mac};
use lettre::message::header::ContentType;
use lettre::transport::smtp::authentication::Credentials;
use lettre::{Message, SmtpTransport, Transport};
use serde::Serialize;
use sha2::Sha256;
use station_domain::{AlertChannel, AlertDelivery, AlertDeliveryPayload};
use station_storage::{AlertChannelSecret, StationStore};
use ureq::Agent;
use ureq::tls::{RootCerts, TlsConfig};

const DEFAULT_LEASE_DURATION_MS: i64 = 30_000;
const DEFAULT_MAXIMUM_ATTEMPTS: u32 = 5;
const DEFAULT_BASE_BACKOFF_MS: i64 = 120_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeliveryEnvelope {
    pub delivery: AlertDelivery,
    pub channel: AlertChannel,
    pub secret: AlertChannelSecret,
    pub payload: AlertDeliveryPayload,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeliveryReceipt {
    pub signature: Option<String>,
}

pub trait AlertTransport: Send + Sync {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String>;
}

pub struct OperatorAlertTransport {
    webhook: WebhookTransport,
    sms: SmsTransport,
    email: EmailTransport,
}

impl OperatorAlertTransport {
    pub fn new(timeout: Duration) -> Self {
        Self {
            webhook: WebhookTransport::new(timeout),
            sms: SmsTransport::new(timeout),
            email: EmailTransport::new(timeout),
        }
    }
}

impl AlertTransport for OperatorAlertTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        match envelope.channel.kind {
            station_domain::AlertChannelKind::Webhook => self.webhook.deliver(envelope),
            station_domain::AlertChannelKind::Sms => self.sms.deliver(envelope),
            station_domain::AlertChannelKind::Email => self.email.deliver(envelope),
        }
    }
}

pub fn run_until(database_path: &Path, stop: Arc<AtomicBool>) -> Result<(), String> {
    let transport = OperatorAlertTransport::new(Duration::from_secs(5));
    let worker = AlertDeliveryWorker::new(database_path, &transport);
    while !stop.load(Ordering::Acquire) {
        let now_unix_ms = i64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| "system clock is before the Unix epoch".to_string())?
                .as_millis(),
        )
        .map_err(|_| "system time exceeds the alerting clock".to_string())?;
        if !worker.run_once(now_unix_ms)? {
            std::thread::sleep(Duration::from_millis(250));
        }
    }
    Ok(())
}

pub struct WebhookTransport {
    agent: Agent,
}

impl WebhookTransport {
    pub fn new(timeout: Duration) -> Self {
        let agent = Agent::config_builder()
            .timeout_global(Some(timeout))
            .max_redirects(0)
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .build()
            .new_agent();
        Self { agent }
    }
}

#[derive(Serialize)]
struct WebhookBody<'a> {
    schema_version: u32,
    delivery_id: &'a str,
    notification_kind: station_domain::AlertNotificationKind,
    event: &'a AlertDeliveryPayload,
}

impl AlertTransport for WebhookTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        envelope.delivery.validate().map_err(str::to_string)?;
        envelope.channel.validate().map_err(str::to_string)?;
        envelope.payload.validate().map_err(str::to_string)?;
        envelope.secret.validate(envelope.channel.kind)?;
        if envelope.channel.kind != station_domain::AlertChannelKind::Webhook
            || envelope.delivery.alert_channel_id != envelope.channel.channel_id
            || envelope.delivery.event_id != envelope.payload.event_id
            || envelope.delivery.notification_kind != envelope.payload.notification_kind
        {
            return Err("webhook delivery envelope is inconsistent".into());
        }
        let body = serde_json::to_vec(&WebhookBody {
            schema_version: 1,
            delivery_id: &envelope.delivery.delivery_id,
            notification_kind: envelope.delivery.notification_kind,
            event: &envelope.payload,
        })
        .map_err(|_| "webhook payload could not be encoded".to_string())?;
        let signature = sign_webhook_payload(envelope.secret.credential.as_bytes(), &body);
        let response = self
            .agent
            .post(&envelope.secret.target)
            .header("Content-Type", "application/json")
            .header("X-TownLight-Delivery", &envelope.delivery.delivery_id)
            .header("X-TownLight-Signature", &signature)
            .send(&body)
            .map_err(webhook_request_error)?;
        if !response.status().is_success() {
            return Err(format!(
                "webhook provider returned HTTP {}",
                response.status().as_u16()
            ));
        }
        Ok(DeliveryReceipt {
            signature: Some(signature),
        })
    }
}

pub struct SmsTransport {
    agent: Agent,
}

impl SmsTransport {
    pub fn new(timeout: Duration) -> Self {
        let agent = Agent::config_builder()
            .timeout_global(Some(timeout))
            .max_redirects(0)
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .build()
            .new_agent();
        Self { agent }
    }
}

impl AlertTransport for SmsTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        envelope.delivery.validate().map_err(str::to_string)?;
        envelope.channel.validate().map_err(str::to_string)?;
        envelope.payload.validate().map_err(str::to_string)?;
        envelope.secret.validate(envelope.channel.kind)?;
        if envelope.channel.kind != station_domain::AlertChannelKind::Sms
            || envelope.delivery.alert_channel_id != envelope.channel.channel_id
            || envelope.delivery.event_id != envelope.payload.event_id
            || envelope.delivery.notification_kind != envelope.payload.notification_kind
        {
            return Err("SMS delivery envelope is inconsistent".into());
        }
        let endpoint = envelope
            .secret
            .endpoint
            .as_deref()
            .ok_or_else(|| "SMS provider endpoint is missing".to_string())?;
        let sender = envelope
            .secret
            .sender
            .as_deref()
            .ok_or_else(|| "SMS provider sender is missing".to_string())?;
        let identity = envelope
            .secret
            .identity
            .as_deref()
            .ok_or_else(|| "SMS provider identity is missing".to_string())?;
        let authorization = format!(
            "Basic {}",
            BASE64_STANDARD.encode(format!("{identity}:{}", envelope.secret.credential))
        );
        let message = build_sms_message(&envelope.payload);
        let form = [
            ("To", envelope.secret.target.as_str()),
            ("From", sender),
            ("Body", message.as_str()),
        ];
        let response = self
            .agent
            .post(endpoint)
            .header("Authorization", &authorization)
            .send_form(form)
            .map_err(sms_request_error)?;
        if !response.status().is_success() {
            return Err(format!(
                "SMS provider returned HTTP {}",
                response.status().as_u16()
            ));
        }
        Ok(DeliveryReceipt { signature: None })
    }
}

pub struct EmailTransport {
    timeout: Duration,
}

impl EmailTransport {
    pub fn new(timeout: Duration) -> Self {
        Self { timeout }
    }
}

impl AlertTransport for EmailTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        envelope.delivery.validate().map_err(str::to_string)?;
        envelope.channel.validate().map_err(str::to_string)?;
        envelope.payload.validate().map_err(str::to_string)?;
        envelope.secret.validate(envelope.channel.kind)?;
        if envelope.channel.kind != station_domain::AlertChannelKind::Email
            || envelope.delivery.alert_channel_id != envelope.channel.channel_id
            || envelope.delivery.event_id != envelope.payload.event_id
            || envelope.delivery.notification_kind != envelope.payload.notification_kind
        {
            return Err("email delivery envelope is inconsistent".into());
        }
        let endpoint = envelope
            .secret
            .endpoint
            .as_deref()
            .ok_or_else(|| "SMTP endpoint is missing".to_string())?;
        let sender = envelope
            .secret
            .sender
            .as_deref()
            .ok_or_else(|| "SMTP sender is missing".to_string())?;
        let identity = envelope
            .secret
            .identity
            .as_deref()
            .ok_or_else(|| "SMTP identity is missing".to_string())?;
        let (secure, host, port) = parse_smtp_endpoint(endpoint)?;
        let severity = alert_severity_label(envelope.payload.severity);
        let condition = alert_condition_label(envelope.payload.condition);
        let subject = format!(
            "[TownLight {severity}] {condition} — {}",
            envelope.payload.resource_ref
        );
        let state = match envelope.payload.notification_kind {
            station_domain::AlertNotificationKind::Firing => "FIRING",
            station_domain::AlertNotificationKind::Realert => "STILL FIRING",
            station_domain::AlertNotificationKind::Resolved => "RESOLVED",
            station_domain::AlertNotificationKind::Test => "TEST",
        };
        let body = format!(
            "TownLight Station operational alert\n\nState: {state}\nSeverity: {severity}\nCondition: {condition}\nResource: {}\n\n{}\n\n{}\n",
            envelope.payload.resource_ref, envelope.payload.summary, envelope.payload.detail
        );
        let message = Message::builder()
            .from(
                sender
                    .parse()
                    .map_err(|_| "SMTP sender address is invalid".to_string())?,
            )
            .to(envelope
                .secret
                .target
                .parse()
                .map_err(|_| "SMTP target address is invalid".to_string())?)
            .subject(subject)
            .header(ContentType::TEXT_PLAIN)
            .body(body)
            .map_err(|_| "alert email could not be constructed".to_string())?;
        let credentials = Credentials::new(identity.into(), envelope.secret.credential.clone());
        let mailer = if secure {
            SmtpTransport::relay(&host)
                .map_err(|_| "secure SMTP relay configuration is invalid".to_string())?
                .port(port)
                .credentials(credentials)
                .timeout(Some(self.timeout))
                .build()
        } else {
            SmtpTransport::builder_dangerous(&host)
                .port(port)
                .credentials(credentials)
                .timeout(Some(self.timeout))
                .build()
        };
        mailer.send(&message).map_err(|_| {
            "SMTP transport failed before the relay accepted the message".to_string()
        })?;
        Ok(DeliveryReceipt { signature: None })
    }
}

fn parse_smtp_endpoint(endpoint: &str) -> Result<(bool, String, u16), String> {
    let (secure, authority, default_port) = if let Some(value) = endpoint.strip_prefix("smtps://") {
        (true, value, 465)
    } else if let Some(value) = endpoint.strip_prefix("smtp://") {
        (false, value, 25)
    } else {
        return Err("SMTP endpoint scheme is invalid".into());
    };
    if authority.is_empty()
        || authority.contains(['/', '?', '#', '@'])
        || authority.chars().any(char::is_whitespace)
    {
        return Err("SMTP endpoint authority is invalid".into());
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port))
            if !host.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()) =>
        {
            let port = port
                .parse::<u16>()
                .ok()
                .filter(|value| *value > 0)
                .ok_or_else(|| "SMTP endpoint port is invalid".to_string())?;
            (host, port)
        }
        Some(_) => return Err("SMTP endpoint authority is invalid".into()),
        None => (authority, default_port),
    };
    let loopback = host.eq_ignore_ascii_case("localhost") || host == "127.0.0.1";
    if !secure && !loopback {
        return Err("remote SMTP endpoints must use smtps".into());
    }
    Ok((secure, host.into(), port))
}

fn alert_severity_label(severity: station_domain::AlertSeverity) -> &'static str {
    match severity {
        station_domain::AlertSeverity::Critical => "CRITICAL",
        station_domain::AlertSeverity::Warning => "WARNING",
        station_domain::AlertSeverity::Info => "INFO",
    }
}

fn alert_condition_label(condition: station_domain::AlertConditionKind) -> &'static str {
    use station_domain::AlertConditionKind;
    match condition {
        AlertConditionKind::OffAir => "off-air",
        AlertConditionKind::EncoderDeath => "encoder-death",
        AlertConditionKind::ServerCrash => "server-crash",
        AlertConditionKind::SchemaDrift => "schema-drift",
        AlertConditionKind::RelayBlocked => "relay-blocked",
        AlertConditionKind::ComplianceProbeFail => "compliance-probe-fail",
        AlertConditionKind::MissingMedia => "missing-media",
        AlertConditionKind::CommitFailure => "commit-failure",
        AlertConditionKind::TakeoverStuck2h => "takeover-stuck-2h",
        AlertConditionKind::AiRuntimeDown => "ai-runtime-down",
        AlertConditionKind::DiskLow => "disk-low",
        AlertConditionKind::ClockSkew => "clock-skew",
        AlertConditionKind::DatabaseUnreachable => "database-unreachable",
        AlertConditionKind::ServiceDown => "service-down",
    }
}

pub fn build_sms_message(payload: &AlertDeliveryPayload) -> String {
    let severity = match payload.severity {
        station_domain::AlertSeverity::Critical => "CRITICAL",
        station_domain::AlertSeverity::Warning => "WARNING",
        station_domain::AlertSeverity::Info => "INFO",
    };
    let message = format!(
        "TownLight {severity}: {} [{}]",
        payload.summary, payload.resource_ref
    );
    if message.chars().count() <= 140 {
        return message;
    }
    let mut shortened: String = message.chars().take(139).collect();
    shortened.push('…');
    shortened
}

pub fn sign_webhook_payload(key: &[u8], payload: &[u8]) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts keys of any size");
    mac.update(payload);
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn webhook_request_error(error: ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(status) => {
            format!("webhook provider returned HTTP {status}")
        }
        _ => "webhook transport failed before a successful provider response".into(),
    }
}

fn sms_request_error(error: ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(status) => {
            format!("SMS provider returned HTTP {status}")
        }
        _ => "SMS transport failed before a successful provider response".into(),
    }
}

pub struct AlertDeliveryWorker<'a, T: AlertTransport + ?Sized> {
    database_path: PathBuf,
    transport: &'a T,
    lease_duration_ms: i64,
    maximum_attempts: u32,
    base_backoff_ms: i64,
}

impl<'a, T: AlertTransport + ?Sized> AlertDeliveryWorker<'a, T> {
    pub fn new(database_path: impl AsRef<Path>, transport: &'a T) -> Self {
        Self {
            database_path: database_path.as_ref().to_path_buf(),
            transport,
            lease_duration_ms: DEFAULT_LEASE_DURATION_MS,
            maximum_attempts: DEFAULT_MAXIMUM_ATTEMPTS,
            base_backoff_ms: DEFAULT_BASE_BACKOFF_MS,
        }
    }

    pub fn run_once(&self, now_unix_ms: i64) -> Result<bool, String> {
        let store = StationStore::open(&self.database_path)?;
        let Some(delivery) = store.claim_due_alert_delivery(now_unix_ms, self.lease_duration_ms)?
        else {
            return Ok(false);
        };
        let delivery_id = delivery.delivery_id.clone();
        let outcome = self
            .prepare_envelope(&store, delivery)
            .and_then(|envelope| {
                self.transport
                    .deliver(&envelope)
                    .map(|receipt| (envelope, receipt))
            });
        match outcome {
            Ok((envelope, receipt)) => {
                store.complete_alert_delivery(
                    &envelope.delivery.delivery_id,
                    now_unix_ms,
                    receipt.signature.as_deref(),
                )?;
            }
            Err(error) => {
                let error = safe_error(&error);
                store.fail_alert_delivery(
                    &delivery_id,
                    now_unix_ms,
                    &error,
                    self.maximum_attempts,
                    self.base_backoff_ms,
                )?;
            }
        }
        Ok(true)
    }

    fn prepare_envelope(
        &self,
        store: &StationStore,
        delivery: AlertDelivery,
    ) -> Result<DeliveryEnvelope, String> {
        let channel = store
            .read_alert_channel(&delivery.alert_channel_id)?
            .ok_or_else(|| "alert delivery channel disappeared".to_string())?;
        if !channel.enabled
            && delivery.notification_kind != station_domain::AlertNotificationKind::Test
        {
            return Err("alert delivery channel is disabled".into());
        }
        let secret = store
            .read_alert_channel_secret(&delivery.alert_channel_id)?
            .ok_or_else(|| "alert delivery secret disappeared".to_string())?;
        let payload = store
            .read_alert_delivery_payload(&delivery.delivery_id)?
            .ok_or_else(|| "alert delivery payload disappeared".to_string())?;
        Ok(DeliveryEnvelope {
            delivery,
            channel,
            secret,
            payload,
        })
    }
}

fn safe_error(error: &str) -> String {
    let mut value: String = error
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .take(1_000)
        .collect();
    if value.trim().is_empty() {
        value = "alert transport failed without a usable error".into();
    }
    value
}
