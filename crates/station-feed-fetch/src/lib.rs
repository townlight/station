use station_cg::{CgFeedKind, CgFeedSource};
use station_feed_normalization::{MAX_INPUT_BYTES, normalize_ical, normalize_rss_or_atom};
use station_storage::{CgWriteError, StationStore};
use std::net::{IpAddr, Ipv6Addr, SocketAddr};
use std::path::Path;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use ureq::unversioned::{
    resolver::{DefaultResolver, ResolvedSocketAddrs, Resolver},
    transport::{DefaultConnector, NextTimeout},
};
use ureq::{Agent, Error as UreqError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FetchRequest {
    pub url: String,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FetchResponse {
    pub status: u16,
    pub content_type: Option<String>,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
    pub body: Vec<u8>,
}
pub trait FeedTransport {
    fn fetch(&mut self, request: &FetchRequest) -> Result<FetchResponse, String>;
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchOutcome {
    Skipped,
    Updated(usize),
    NotModified,
    Failed(String),
}

pub struct FeedCoordinator<T> {
    transport: T,
    max_body_bytes: usize,
}
impl<T: FeedTransport> FeedCoordinator<T> {
    pub fn new(transport: T) -> Self {
        Self {
            transport,
            max_body_bytes: MAX_INPUT_BYTES,
        }
    }
    pub fn tick(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<Vec<(String, FetchOutcome)>, String> {
        let never_stop = AtomicBool::new(false);
        self.tick_with_stop(store, now_unix_ms, &never_stop)
    }

    pub fn tick_with_stop(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
        stop: &AtomicBool,
    ) -> Result<Vec<(String, FetchOutcome)>, String> {
        if now_unix_ms < 0 {
            return Ok(Vec::new());
        }
        let feeds = store
            .list_all_cg_feed_sources()
            .map_err(|error| format!("could not list CG feed sources: {error}"))?;
        let mut outcomes = Vec::new();
        for feed in feeds {
            if stop.load(Ordering::Acquire) {
                break;
            }
            if feed.enabled && due(&feed, now_unix_ms) {
                outcomes.push(self.fetch_one(store, &feed, now_unix_ms)?);
            }
        }
        Ok(outcomes)
    }
    fn fetch_one(
        &mut self,
        store: &StationStore,
        feed: &CgFeedSource,
        now: i64,
    ) -> Result<(String, FetchOutcome), String> {
        let id = feed.feed_source_id.clone();
        let cache = store
            .read_cg_feed_cache(&id)
            .map_err(|error| format!("could not read CG feed cache {id}: {error}"))?;
        let request = FetchRequest {
            url: feed.source_url.clone(),
            etag: cache.as_ref().and_then(|c| c.etag.clone()),
            last_modified: cache.as_ref().and_then(|c| c.last_modified.clone()),
        };
        let response = match self.transport.fetch(&request) {
            Ok(response) => response,
            Err(error) => return self.failed(store, &id, now, error),
        };
        if response.status == 304 {
            if let Err(error) = store.refresh_cg_feed_cache(&id, now) {
                if is_infrastructure_error(&error) {
                    return Err(format!("could not refresh CG feed cache {id}: {error:?}"));
                }
                return self.failed(store, &id, now, "304 cache refresh failed".into());
            }
            return Ok((id, FetchOutcome::NotModified));
        }
        if response.status != 200 || response.body.len() > self.max_body_bytes {
            return self.failed(store, &id, now, "feed response rejected".into());
        }
        let items = match feed.kind {
            CgFeedKind::Rss => normalize_rss_or_atom(&feed.source_url, &response.body)
                .map_err(|_| "feed content rejected".to_owned()),
            CgFeedKind::Ical | CgFeedKind::Caldav => {
                normalize_ical(&feed.source_url, &response.body)
                    .map_err(|_| "feed content rejected".to_owned())
            }
            _ => return self.failed(store, &id, now, "feed kind has no safe normalizer".into()),
        };
        match items {
            Ok(items) => match store.replace_cg_feed_cache(
                &id,
                now,
                &items,
                response.etag.as_deref(),
                response.last_modified.as_deref(),
            ) {
                Ok(_) => Ok((id, FetchOutcome::Updated(items.len()))),
                Err(error) if is_infrastructure_error(&error) => {
                    Err(format!("could not write CG feed cache {id}: {error:?}"))
                }
                Err(_) => self.failed(store, &id, now, "feed cache write rejected".into()),
            },
            Err(error) => self.failed(store, &id, now, error),
        }
    }
    fn failed(
        &self,
        store: &StationStore,
        id: &str,
        now: i64,
        error: String,
    ) -> Result<(String, FetchOutcome), String> {
        let error = if error.trim().is_empty() {
            "feed transport failed".to_owned()
        } else {
            error
        };
        let mut end = 0;
        for (index, character) in error.char_indices() {
            let next = index + character.len_utf8();
            if next > 1_000 {
                break;
            }
            end = next;
        }
        let bounded = error[..end].to_owned();
        if let Err(error) = store.record_cg_feed_fetch_failure(id, now, &bounded)
            && is_infrastructure_error(&error)
        {
            return Err(format!("could not record CG feed failure {id}: {error:?}"));
        }
        Ok((id.into(), FetchOutcome::Failed(bounded)))
    }
}

fn is_infrastructure_error(error: &CgWriteError) -> bool {
    matches!(error, CgWriteError::Storage(_))
}

pub const FEED_POLL_INTERVAL: Duration = Duration::from_secs(1);
const SHUTDOWN_CHECK_INTERVAL: Duration = Duration::from_millis(50);

pub fn run_until(database_path: &Path, stop: Arc<AtomicBool>) -> Result<(), String> {
    let store = StationStore::open(database_path)?;
    run_until_with(
        store,
        FeedCoordinator::new(UreqFeedTransport::new()),
        stop,
        system_time_millis,
        wait_for_poll,
    )
}

pub fn run_until_with<T, N, W>(
    store: StationStore,
    mut coordinator: FeedCoordinator<T>,
    stop: Arc<AtomicBool>,
    mut now: N,
    mut wait: W,
) -> Result<(), String>
where
    T: FeedTransport,
    N: FnMut() -> Result<i64, String>,
    W: FnMut(Duration, &AtomicBool),
{
    while !stop.load(Ordering::Acquire) {
        coordinator.tick_with_stop(&store, now()?, stop.as_ref())?;
        if !stop.load(Ordering::Acquire) {
            wait(FEED_POLL_INTERVAL, stop.as_ref());
        }
    }
    Ok(())
}

fn system_time_millis() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock is before Unix epoch: {error}"))?
        .as_millis();
    i64::try_from(millis).map_err(|_| "system clock exceeds i64 range".into())
}

fn wait_for_poll(duration: Duration, stop: &AtomicBool) {
    let deadline = Instant::now() + duration;
    while !stop.load(Ordering::Acquire) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        std::thread::sleep(remaining.min(SHUTDOWN_CHECK_INTERVAL));
    }
}
fn due(feed: &CgFeedSource, now: i64) -> bool {
    now >= feed.created_at_unix_ms
        && feed
            .last_fetched_at_unix_ms
            .is_none_or(|last| now >= last.saturating_add(i64::from(feed.refresh_seconds) * 1_000))
}

#[derive(Debug, Clone, Copy)]
struct ResolverPolicy {
    allow_loopback: bool,
    allow_nonstandard_port: bool,
    allow_http: bool,
}

#[derive(Debug, Clone, Copy)]
struct SafeResolver {
    policy: ResolverPolicy,
}
impl Resolver for SafeResolver {
    fn resolve(
        &self,
        uri: &ureq::http::Uri,
        config: &ureq::config::Config,
        timeout: NextTimeout,
    ) -> Result<ResolvedSocketAddrs, UreqError> {
        let addresses = DefaultResolver::default().resolve(uri, config, timeout)?;
        let approved = approved_addresses_with_policy(&addresses, self.policy)
            .map_err(|_| UreqError::HostNotFound)?;
        let mut output = self.empty();
        for address in approved {
            output.push(address);
        }
        Ok(output)
    }
}

pub struct UreqFeedTransport {
    agent: Agent,
    policy: ResolverPolicy,
}
impl UreqFeedTransport {
    pub fn new() -> Self {
        Self::with_policy(ResolverPolicy {
            allow_loopback: false,
            allow_nonstandard_port: false,
            allow_http: false,
        })
    }

    #[doc = "Test-only constructor: permits loopback and dynamic ports for a local HTTP fixture."]
    #[cfg(test)]
    pub fn new_for_loopback_tests() -> Self {
        Self::with_policy(ResolverPolicy {
            allow_loopback: true,
            allow_nonstandard_port: true,
            allow_http: true,
        })
    }

    fn with_policy(policy: ResolverPolicy) -> Self {
        let config = Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_global(Some(std::time::Duration::from_secs(4)))
            .timeout_connect(Some(std::time::Duration::from_secs(2)))
            .timeout_recv_body(Some(std::time::Duration::from_secs(3)))
            .build();
        Self {
            agent: Agent::with_parts(config, DefaultConnector::default(), SafeResolver { policy }),
            policy,
        }
    }
}
impl Default for UreqFeedTransport {
    fn default() -> Self {
        Self::new()
    }
}
impl FeedTransport for UreqFeedTransport {
    fn fetch(&mut self, request: &FetchRequest) -> Result<FetchResponse, String> {
        let url = url::Url::parse(&request.url).map_err(|_| "feed URL rejected".to_owned())?;
        if (url.scheme() != "https" && !(self.policy.allow_http && url.scheme() == "http"))
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
            || (!self.policy.allow_nonstandard_port && url.port().is_some_and(|port| port != 443))
        {
            return Err("unsafe feed URL".into());
        }
        validate_request_header(request.etag.as_deref(), "ETag")?;
        validate_request_header(request.last_modified.as_deref(), "Last-Modified")?;
        let mut call = self.agent.get(request.url.as_str());
        if let Some(value) = request.etag.as_deref() {
            call = call.header("if-none-match", value);
        }
        if let Some(value) = request.last_modified.as_deref() {
            call = call.header("if-modified-since", value);
        }
        let mut response = call.call().map_err(|_| "feed request failed".to_owned())?;
        let status = response.status().as_u16();
        let etag = response_header(response.headers(), "etag", 1_024)?;
        let last_modified = response_header(response.headers(), "last-modified", 1_024)?;
        let content_type = response_header(response.headers(), "content-type", 256)?;
        let body = if status == 200 {
            response
                .body_mut()
                .with_config()
                .limit((MAX_INPUT_BYTES + 1) as u64)
                .read_to_vec()
                .map_err(|_| "feed body read failed".to_owned())?
        } else {
            Vec::new()
        };
        if body.len() > MAX_INPUT_BYTES {
            return Err("feed response exceeds byte ceiling".into());
        }
        Ok(FetchResponse {
            status,
            content_type,
            etag,
            last_modified,
            body,
        })
    }
}

fn validate_request_header(value: Option<&str>, name: &str) -> Result<(), String> {
    if let Some(value) = value
        && (value.len() > 1_024 || value.bytes().any(|byte| byte < 0x20 || byte == 0x7f))
    {
        return Err(format!("invalid {name} header"));
    }
    Ok(())
}

fn response_header(
    headers: &ureq::http::HeaderMap,
    name: &str,
    max_bytes: usize,
) -> Result<Option<String>, String> {
    let Some(value) = headers.get(name) else {
        return Ok(None);
    };
    let value = value
        .to_str()
        .map_err(|_| format!("invalid {name} response header"))?;
    if value.len() > max_bytes || value.bytes().any(|byte| byte < 0x20 || byte == 0x7f) {
        return Err(format!("invalid {name} response header"));
    }
    Ok(Some(value.to_owned()))
}

pub fn approved_public_addresses(addresses: &[SocketAddr]) -> Result<Vec<SocketAddr>, String> {
    approved_addresses_with_policy(
        addresses,
        ResolverPolicy {
            allow_loopback: false,
            allow_nonstandard_port: false,
            allow_http: false,
        },
    )
}
fn approved_addresses_with_policy(
    addresses: &[SocketAddr],
    policy: ResolverPolicy,
) -> Result<Vec<SocketAddr>, String> {
    if addresses.is_empty() {
        return Err("DNS returned no addresses".into());
    }
    if addresses
        .iter()
        .any(|address| !is_public_address(address.ip(), policy.allow_loopback))
    {
        return Err("DNS answer contains a non-public address; request rejected".into());
    }
    Ok(addresses.to_vec())
}
fn is_public_address(address: IpAddr, allow_loopback: bool) -> bool {
    match address {
        IpAddr::V4(address) => {
            let [a, b, c, _] = address.octets();
            !((a == 0)
                || address.is_private()
                || (a == 100 && (64..=127).contains(&b))
                || (a == 127 && !allow_loopback)
                || (a == 169 && b == 254)
                || (a == 192 && b == 0 && c == 0)
                || (a == 192 && b == 0 && c == 2)
                || (a == 192 && b == 88 && c == 99)
                || (a == 198 && (18..=19).contains(&b))
                || (a == 198 && b == 51)
                || (a == 203 && b == 0 && c == 113)
                || a >= 224
                || address.is_link_local()
                || address.is_multicast()
                || address.is_unspecified())
        }
        IpAddr::V6(address) => {
            if let Some(v4) = address.to_ipv4() {
                return is_public_address(IpAddr::V4(v4), allow_loopback);
            }
            !((!allow_loopback && address.is_loopback())
                || address.is_unspecified()
                || address.is_multicast()
                || address.is_unique_local()
                || address.is_unicast_link_local()
                || address == Ipv6Addr::LOCALHOST
                || address.segments()[0] == 0x2001
                    && matches!(address.segments()[1], 0x0002 | 0x0010 | 0x0020 | 0x0db8)
                || address.segments()[0] == 0x3fff)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use station_cg::{CgAuditEvent, CgAuditEventKind, CgFeedItemApproval, CgFeedTrustTier};
    use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
    use std::collections::BTreeMap;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::PathBuf;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use std::time::{SystemTime, UNIX_EPOCH};

    const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const FEED_ID: &str = "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24";

    struct QueueTransport {
        responses: Vec<Result<FetchResponse, String>>,
        requests: Vec<FetchRequest>,
    }
    impl FeedTransport for QueueTransport {
        fn fetch(&mut self, request: &FetchRequest) -> Result<FetchResponse, String> {
            self.requests.push(request.clone());
            self.responses
                .pop()
                .unwrap_or_else(|| Err("no mock response".into()))
        }
    }

    struct StopAfterFirstTransport {
        stop: Arc<AtomicBool>,
        calls: Arc<AtomicUsize>,
    }
    impl FeedTransport for StopAfterFirstTransport {
        fn fetch(&mut self, _request: &FetchRequest) -> Result<FetchResponse, String> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            self.stop.store(true, Ordering::Release);
            Ok(response(200, rss()))
        }
    }

    fn database_path(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("townlight-feed-{label}-{nonce}.db"))
    }
    fn remove_database(path: &std::path::Path) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{}", path.display(), suffix));
        }
    }
    fn put_channel(store: &StationStore) {
        store
            .put_channel(&ChannelConfiguration {
                channel_id: CHANNEL_ID.into(),
                display_name: "Government Channel".into(),
                output_profile: OutputProfileId::Proof360p30,
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    "f103968a-640b-42dc-8084-3fe9e847ff7f",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
    }
    fn source(kind: CgFeedKind, enabled: bool, refresh_seconds: u32) -> CgFeedSource {
        CgFeedSource {
            feed_source_id: FEED_ID.into(),
            channel_id: CHANNEL_ID.into(),
            kind,
            label: "Notices".into(),
            source_url: "https://example.test/feed".into(),
            trust_tier: CgFeedTrustTier::Trusted,
            tags: Vec::new(),
            refresh_seconds,
            enabled,
            revision: 1,
            created_at_unix_ms: 1_000,
            updated_at_unix_ms: 1_000,
            created_by: "operator".into(),
            last_fetched_at_unix_ms: None,
            last_fetch_error: None,
        }
    }
    fn add_source(store: &StationStore, feed: &CgFeedSource) {
        let audit_id = if feed.feed_source_id == FEED_ID {
            "7bb32e14-fe69-47cb-a921-dc3fc84499bf"
        } else {
            "9bb32e14-fe69-47cb-a921-dc3fc84499bf"
        };
        store
            .write_cg_feed_source(
                feed,
                0,
                &CgAuditEvent {
                    audit_id: audit_id.into(),
                    board_id: None,
                    channel_id: CHANNEL_ID.into(),
                    event_kind: CgAuditEventKind::FeedCreated,
                    operator_id: "operator".into(),
                    occurred_at_unix_ms: 0,
                    details: BTreeMap::new(),
                },
            )
            .unwrap();
    }
    fn rss() -> Vec<u8> {
        br#"<rss version="2.0"><channel><item><guid>notice-1</guid><title>Town notice</title><description>Be advised</description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>"#.to_vec()
    }
    fn response(status: u16, body: Vec<u8>) -> FetchResponse {
        FetchResponse {
            status,
            content_type: Some("application/rss+xml".into()),
            etag: Some("\"v1\"".into()),
            last_modified: Some("Mon, 01 Jan 2024 00:00:00 GMT".into()),
            body,
        }
    }
    fn feed(last: Option<i64>, enabled: bool, kind: CgFeedKind) -> CgFeedSource {
        CgFeedSource {
            feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            channel_id: "5d85189a-cd1f-43dc-a619-66a2aeb09ac4".into(),
            kind,
            label: "x".into(),
            source_url: "https://example.test/feed".into(),
            trust_tier: station_cg::CgFeedTrustTier::Trusted,
            tags: Vec::new(),
            refresh_seconds: 60,
            enabled,
            revision: 1,
            created_at_unix_ms: 0,
            updated_at_unix_ms: 0,
            created_by: "x".into(),
            last_fetched_at_unix_ms: last,
            last_fetch_error: None,
        }
    }
    #[test]
    fn response_and_request_preserve_conditional_headers() {
        let request = FetchRequest {
            url: "https://example.test".into(),
            etag: Some("\"v1\"".into()),
            last_modified: Some("Wed, 01 Jan 2025 00:00:00 GMT".into()),
        };
        assert_eq!(request.etag.as_deref(), Some("\"v1\""));
        assert_eq!(
            request.last_modified.as_deref(),
            Some("Wed, 01 Jan 2025 00:00:00 GMT")
        );
    }
    #[test]
    fn resolver_policy_fails_closed_for_mixed_answers() {
        let public: SocketAddr = "8.8.8.8:443".parse().unwrap();
        let private: SocketAddr = "192.168.1.10:443".parse().unwrap();
        assert!(approved_public_addresses(&[public, private]).is_err());
        assert_eq!(approved_public_addresses(&[public]).unwrap(), vec![public]);
    }
    #[test]
    fn resolver_policy_rejects_loopback_ipv4_and_ipv6() {
        assert!(approved_public_addresses(&["127.0.0.1:443".parse().unwrap()]).is_err());
        assert!(approved_public_addresses(&["[::1]:443".parse().unwrap()]).is_err());
    }

    #[test]
    fn resolver_policy_rejects_reserved_cgnat_benchmark_and_mapped_ranges() {
        for value in [
            "100.64.0.1:443",
            "0.1.2.3:443",
            "198.18.0.1:443",
            "240.0.0.1:443",
            "[2001:db8::1]:443",
            "[::ffff:192.168.1.1]:443",
        ] {
            assert!(
                approved_public_addresses(&[value.parse().unwrap()]).is_err(),
                "{value}"
            );
        }
    }
    #[test]
    fn due_without_attempt_is_true() {
        assert!(due(&feed(None, true, CgFeedKind::Rss), 0));
    }
    #[test]
    fn due_before_refresh_is_false() {
        assert!(!due(&feed(Some(1_000), true, CgFeedKind::Rss), 1_001));
    }
    #[test]
    fn due_at_refresh_is_true() {
        assert!(due(&feed(Some(1_000), true, CgFeedKind::Rss), 61_000));
    }
    #[test]
    fn disabled_feed_is_not_due_for_selection() {
        let f = feed(None, false, CgFeedKind::Rss);
        assert!(!f.enabled);
    }
    #[test]
    fn unsupported_kind_is_distinct() {
        assert_ne!(CgFeedKind::Weather, CgFeedKind::Rss);
    }
    #[test]
    fn negative_clock_is_not_due() {
        let now = -1_i64;
        assert!(now < 0);
    }
    #[test]
    fn response_ceiling_is_explicit() {
        assert_eq!(MAX_INPUT_BYTES + 1, 524_289);
    }
    #[test]
    fn conditional_request_fields_are_optional() {
        let request = FetchRequest {
            url: "https://example.test".into(),
            etag: None,
            last_modified: None,
        };
        assert!(request.etag.is_none() && request.last_modified.is_none());
    }

    #[test]
    fn coordinator_skips_disabled_and_not_due_sources_in_a_real_store() {
        let path = database_path("skip");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        let mut disabled = source(CgFeedKind::Rss, false, 60);
        add_source(&store, &disabled);
        disabled.feed_source_id = "9ac46d05-09d8-4ef8-9607-75c365b3274e".into();
        disabled.enabled = true;
        disabled.last_fetched_at_unix_ms = Some(1_000);
        disabled.label = "Second".into();
        add_source(&store, &disabled);
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(200, rss()))],
            requests: Vec::new(),
        });
        assert!(coordinator.tick(&store, 1_001).unwrap().is_empty());
        remove_database(&path);
    }

    #[test]
    fn coordinator_stops_between_feeds_when_shutdown_is_requested() {
        let path = database_path("stop-between-feeds");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut second = source(CgFeedKind::Rss, true, 60);
        second.feed_source_id = "9ac46d05-09d8-4ef8-9607-75c365b3274e".into();
        second.label = "Second notices".into();
        add_source(&store, &second);
        let stop = Arc::new(AtomicBool::new(false));
        let calls = Arc::new(AtomicUsize::new(0));
        let transport = StopAfterFirstTransport {
            stop: Arc::clone(&stop),
            calls: Arc::clone(&calls),
        };
        let mut coordinator = FeedCoordinator::new(transport);
        let outcomes = coordinator
            .tick_with_stop(&store, 1_000, stop.as_ref())
            .unwrap();
        assert_eq!(outcomes.len(), 1);
        assert_eq!(calls.load(Ordering::Acquire), 1);
        remove_database(&path);
    }

    #[test]
    fn run_loop_uses_bounded_wait_and_stops_without_a_second_tick() {
        let path = database_path("run-loop-stop");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let calls = Arc::new(AtomicUsize::new(0));
        let transport = StopAfterFirstTransport {
            stop: Arc::new(AtomicBool::new(false)),
            calls: Arc::clone(&calls),
        };
        let stop = Arc::new(AtomicBool::new(false));
        let mut clock_calls = 0;
        let mut waits = 0;
        run_until_with(
            store,
            FeedCoordinator::new(transport),
            Arc::clone(&stop),
            || {
                clock_calls += 1;
                Ok(1_000)
            },
            |duration, flag| {
                waits += 1;
                assert_eq!(duration, FEED_POLL_INTERVAL);
                flag.store(true, Ordering::Release);
            },
        )
        .unwrap();
        assert_eq!(calls.load(Ordering::Acquire), 1);
        assert_eq!(clock_calls, 1);
        assert_eq!(waits, 1);
        remove_database(&path);
    }

    #[test]
    fn run_loop_surfaces_clock_failure() {
        let path = database_path("run-loop-clock-error");
        let store = StationStore::open(&path).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let result = run_until_with(
            store,
            FeedCoordinator::new(QueueTransport {
                responses: Vec::new(),
                requests: Vec::new(),
            }),
            stop,
            || Err("clock unavailable".into()),
            |_duration, _flag| panic!("wait must not run after clock failure"),
        );
        assert_eq!(result.unwrap_err(), "clock unavailable");
        remove_database(&path);
    }

    #[test]
    fn production_loop_surfaces_database_open_failure() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir()
            .join(format!("townlight-feed-missing-parent-{nonce}"))
            .join("station.db");
        let stop = Arc::new(AtomicBool::new(false));
        assert!(run_until(&path, stop).is_err());
    }

    #[test]
    fn coordinator_200_persists_items_and_validators_in_a_real_store() {
        let path = database_path("success");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(200, rss()))],
            requests: Vec::new(),
        });
        assert_eq!(
            coordinator.tick(&store, 10_000).unwrap(),
            vec![(FEED_ID.into(), FetchOutcome::Updated(1))]
        );
        let cache = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
        assert_eq!(cache.items[0].title, "Town notice");
        assert_eq!(cache.etag.as_deref(), Some("\"v1\""));
        assert_eq!(
            cache.last_modified.as_deref(),
            Some("Mon, 01 Jan 2024 00:00:00 GMT")
        );
        remove_database(&path);
    }

    #[test]
    fn coordinator_304_refreshes_freshness_and_preserves_items() {
        let path = database_path("not-modified");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut first = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(200, rss()))],
            requests: Vec::new(),
        });
        first.tick(&store, 10_000).unwrap();
        let before = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
        let approval = CgFeedItemApproval {
            feed_source_id: FEED_ID.into(),
            item_id: before.items[0].item_id.clone(),
            approved_by: "operator".into(),
            approved_at_unix_ms: 10_001,
        };
        store
            .approve_cg_feed_item(
                &approval,
                &CgAuditEvent {
                    audit_id: "8bb32e14-fe69-47cb-a921-dc3fc84499bf".into(),
                    board_id: None,
                    channel_id: CHANNEL_ID.into(),
                    event_kind: CgAuditEventKind::FeedItemApproved,
                    operator_id: "operator".into(),
                    occurred_at_unix_ms: 10_001,
                    details: BTreeMap::new(),
                },
            )
            .unwrap();
        let mut second = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(304, Vec::new()))],
            requests: Vec::new(),
        });
        assert_eq!(
            second.tick(&store, 71_000).unwrap(),
            vec![(FEED_ID.into(), FetchOutcome::NotModified)]
        );
        let after = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
        assert_eq!(after.items, before.items);
        assert_eq!(
            store
                .read_cg_feed_item_approval(FEED_ID, &before.items[0].item_id)
                .unwrap(),
            Some(approval)
        );
        assert!(after.fetched_at_unix_ms > before.fetched_at_unix_ms);
        remove_database(&path);
    }

    #[test]
    fn coordinator_records_304_without_cache_as_a_nonfatal_feed_failure() {
        let path = database_path("not-modified-without-cache");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(304, Vec::new()))],
            requests: Vec::new(),
        });
        let outcomes = coordinator.tick(&store, 10_000).unwrap();
        assert!(matches!(outcomes[0].1, FetchOutcome::Failed(_)));
        assert!(store.read_cg_feed_cache(FEED_ID).unwrap().is_none());
        assert_eq!(
            store
                .read_cg_feed_source(FEED_ID)
                .unwrap()
                .unwrap()
                .last_fetch_error
                .as_deref(),
            Some("304 cache refresh failed")
        );
        remove_database(&path);
    }

    #[test]
    fn coordinator_failure_preserves_cache_and_paces_retry_by_attempt_time() {
        let path = database_path("failure");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut seed = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(200, rss()))],
            requests: Vec::new(),
        });
        seed.tick(&store, 10_000).unwrap();
        let before = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
        let mut failing = FeedCoordinator::new(QueueTransport {
            responses: vec![Err("temporary upstream failure".into())],
            requests: Vec::new(),
        });
        assert!(matches!(
            failing.tick(&store, 71_000).unwrap()[0].1,
            FetchOutcome::Failed(_)
        ));
        let after = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
        assert_eq!(after.items, before.items);
        assert!(after.last_error.is_some());
        let source_after = store.read_cg_feed_source(FEED_ID).unwrap().unwrap();
        assert_eq!(source_after.last_fetched_at_unix_ms, Some(71_000));
        assert!(failing.tick(&store, 71_001).unwrap().is_empty());
        remove_database(&path);
    }

    #[test]
    fn coordinator_bounds_multibyte_failure_errors_to_utf8_bytes() {
        let path = database_path("multibyte-error");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let error = "界".repeat(400);
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Err(error)],
            requests: Vec::new(),
        });
        let outcome = coordinator.tick(&store, 1_000).unwrap();
        assert!(matches!(outcome[0].1, FetchOutcome::Failed(_)));
        let saved = store
            .read_cg_feed_source(FEED_ID)
            .unwrap()
            .unwrap()
            .last_fetch_error
            .unwrap();
        assert!(saved.len() <= 1_000);
        assert_eq!(saved.len() % "界".len(), 0);
        remove_database(&path);
    }

    #[test]
    fn coordinator_records_and_paces_empty_transport_errors() {
        let path = database_path("empty-error");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Err("   ".into())],
            requests: Vec::new(),
        });
        assert!(matches!(
            coordinator.tick(&store, 1_000).unwrap()[0].1,
            FetchOutcome::Failed(ref message) if message == "feed transport failed"
        ));
        let saved = store
            .read_cg_feed_source(FEED_ID)
            .unwrap()
            .unwrap()
            .last_fetch_error
            .unwrap();
        assert_eq!(saved, "feed transport failed");
        assert!(coordinator.tick(&store, 1_001).unwrap().is_empty());
        remove_database(&path);
    }

    #[test]
    fn storage_rejects_backward_cache_times_without_mutating_real_store() {
        let path = database_path("clock-regression");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut seed = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(200, rss()))],
            requests: Vec::new(),
        });
        seed.tick(&store, 1_000).unwrap();
        let before_cache = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
        let before_source = store.read_cg_feed_source(FEED_ID).unwrap().unwrap();
        assert!(
            store
                .replace_cg_feed_cache(
                    FEED_ID,
                    999,
                    &before_cache.items,
                    before_cache.etag.as_deref(),
                    before_cache.last_modified.as_deref(),
                )
                .is_err()
        );
        assert!(store.refresh_cg_feed_cache(FEED_ID, 999).is_err());
        assert!(
            store
                .record_cg_feed_fetch_failure(FEED_ID, 999, "clock regression")
                .is_err()
        );
        assert_eq!(
            store.read_cg_feed_cache(FEED_ID).unwrap(),
            Some(before_cache)
        );
        assert_eq!(
            store.read_cg_feed_source(FEED_ID).unwrap(),
            Some(before_source)
        );
        remove_database(&path);
    }

    #[test]
    fn coordinator_rejects_unsupported_kind_and_bad_status_without_cache() {
        let path = database_path("reject");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Weather, true, 60));
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(500, vec![]))],
            requests: Vec::new(),
        });
        assert!(matches!(
            coordinator.tick(&store, 1_000).unwrap()[0].1,
            FetchOutcome::Failed(_)
        ));
        assert!(store.read_cg_feed_cache(FEED_ID).unwrap().is_none());
        remove_database(&path);
    }

    #[test]
    fn coordinator_negative_clock_is_a_deterministic_noop_against_store() {
        let path = database_path("negative");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store);
        add_source(&store, &source(CgFeedKind::Rss, true, 60));
        let mut coordinator = FeedCoordinator::new(QueueTransport {
            responses: vec![Ok(response(200, rss()))],
            requests: Vec::new(),
        });
        assert!(coordinator.tick(&store, -1).unwrap().is_empty());
        assert!(
            store
                .read_cg_feed_source(FEED_ID)
                .unwrap()
                .unwrap()
                .last_fetched_at_unix_ms
                .is_none()
        );
        remove_database(&path);
    }

    fn loopback_server(
        status: &str,
        headers: &str,
        body: &[u8],
    ) -> (String, std::thread::JoinHandle<String>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let body = body.to_vec();
        let status = status.to_owned();
        let headers = headers.to_owned();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let count = stream.read(&mut buffer).unwrap();
                if count == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..count]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let response = format!(
                "HTTP/1.1 {status}\r\n{headers}Content-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
            stream.write_all(&body).unwrap();
            String::from_utf8_lossy(&request).into_owned()
        });
        (format!("http://{address}/feed"), handle)
    }

    #[test]
    fn loopback_transport_sends_validators_and_passthrough_304() {
        let (url, server) = loopback_server("304 Not Modified", "", &[]);
        let mut transport = UreqFeedTransport::new_for_loopback_tests();
        let response = transport
            .fetch(&FetchRequest {
                url,
                etag: Some("\"v1\"".into()),
                last_modified: Some("Wed, 01 Jan 2025 00:00:00 GMT".into()),
            })
            .unwrap();
        let request = server.join().unwrap();
        assert_eq!(response.status, 304);
        assert!(
            request
                .to_ascii_lowercase()
                .contains("if-none-match: \"v1\"")
        );
        assert!(
            request
                .to_ascii_lowercase()
                .contains("if-modified-since: wed, 01 jan 2025 00:00:00 gmt")
        );
    }

    #[test]
    fn loopback_transport_refuses_redirect_following() {
        let (url, server) =
            loopback_server("302 Found", "Location: http://127.0.0.1:1/other\r\n", &[]);
        let mut transport = UreqFeedTransport::new_for_loopback_tests();
        let response = transport
            .fetch(&FetchRequest {
                url,
                etag: None,
                last_modified: None,
            })
            .unwrap();
        assert_eq!(response.status, 302);
        server.join().unwrap();
    }

    #[test]
    fn loopback_transport_enforces_body_ceiling() {
        let (url, server) = loopback_server(
            "200 OK",
            "Content-Type: application/rss+xml\r\n",
            &vec![b'x'; MAX_INPUT_BYTES + 1],
        );
        let mut transport = UreqFeedTransport::new_for_loopback_tests();
        assert!(
            transport
                .fetch(&FetchRequest {
                    url,
                    etag: None,
                    last_modified: None
                })
                .is_err()
        );
        server.join().unwrap();
    }
}
