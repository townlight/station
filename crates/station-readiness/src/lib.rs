//! Pure, deterministic Station Box readiness contracts.
//!
//! Platform-specific probing is intentionally outside this crate. Callers build a
//! [`StationBoxProbeInput`] from installed-machine evidence and pass it to
//! [`derive_station_box_profile`].

use serde::{Deserialize, Serialize};

mod headend;

pub use headend::*;

const SCHEMA_VERSION: u32 = 2;
const MAX_TEXT: usize = 200;
const MAX_FACTORY_NAME: usize = 120;
const MAX_LIST_ITEMS: usize = 256;
const DEFAULT_MAX_PROBE_AGE_MS: i64 = 300_000;
pub const REFERENCE_STATION_CATALOG_VERSION: u32 = 1;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StationBoxProbeInput {
    pub generated_at_unix_ms: i64,
    pub probe_collected_at_unix_ms: i64,
    #[serde(default = "default_max_probe_age_ms")]
    pub max_probe_age_ms: i64,
    pub product_version: String,
    pub source_commit: Option<String>,
    #[serde(default)]
    pub station_id: Option<String>,
    #[serde(default)]
    pub station_name: Option<String>,
    #[serde(default)]
    pub timezone: Option<String>,
    pub hardware: HardwareProbeInput,
    pub engine: EngineProbeInput,
    pub clock: ClockProbeInput,
    pub network: NetworkProbeInput,
    pub backup: BackupProbeInput,
    pub release: ReleaseProbeInput,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HardwareProbeInput {
    pub logical_cpus: u32,
    pub ram_total_gb: f64,
    pub media_disk_free_gb: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngineProbeInput {
    pub gstreamer_version: Option<String>,
    pub required_factories: Vec<String>,
    pub available_factories: Vec<String>,
    pub available_encoders: Vec<String>,
    pub playout: ReadinessState,
    pub runtime: ReadinessState,
    pub output: ReadinessState,
    pub udp: CapabilitySupport,
    pub srt: CapabilitySupport,
    pub sdi: CapabilitySupport,
    pub ndi: CapabilitySupport,
    pub interlaced: CapabilitySupport,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClockProbeInput {
    pub configured_timezone: Option<String>,
    pub utc_offset_seconds: Option<i32>,
    pub sync_status: ClockSyncStatus,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetworkProbeInput {
    pub state: ReadinessState,
    pub hostname: Option<String>,
    pub interface_up: bool,
    pub dns_working: Option<bool>,
    pub outbound_udp: CapabilitySupport,
    pub outbound_srt: CapabilitySupport,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackupProbeInput {
    pub state: ReadinessState,
    pub destination_configured: bool,
    pub writable: Option<bool>,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseProbeInput {
    pub state: ReadinessState,
    pub candidate_verified: bool,
    pub update_available: bool,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StationBoxProfile {
    pub schema_version: u32,
    pub generated_at_unix_ms: i64,
    pub product_version: String,
    pub source_commit: Option<String>,
    pub station_id: Option<String>,
    pub station_name: Option<String>,
    pub timezone: Option<String>,
    pub hardware: HardwareReadiness,
    pub engine: EngineReadiness,
    pub clock: ClockReadiness,
    pub network: NetworkReadiness,
    pub backup: BackupReadiness,
    pub release: ReleaseReadiness,
    pub engine_tier_verdict: EngineTierVerdict,
    pub reference_station_catalog: ReferenceStationCatalog,
    pub peg_readiness: PegReadinessRollup,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HardwareReadiness {
    pub logical_cpus: u32,
    pub ram_total_gb: f64,
    pub media_disk_free_gb: f64,
    pub state: ReadinessState,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngineReadiness {
    pub gstreamer_version: Option<String>,
    pub required_factories: Vec<String>,
    pub missing_factories: Vec<String>,
    pub available_encoders: Vec<String>,
    pub udp: CapabilitySupport,
    pub srt: CapabilitySupport,
    pub sdi: CapabilitySupport,
    pub ndi: CapabilitySupport,
    pub interlaced: CapabilitySupport,
    pub playout: ReadinessState,
    pub runtime: ReadinessState,
    pub output: ReadinessState,
    pub state: ReadinessState,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClockReadiness {
    pub configured_timezone: Option<String>,
    pub utc_offset_seconds: Option<i32>,
    pub sync_status: ClockSyncStatus,
    pub state: ReadinessState,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetworkReadiness {
    pub state: ReadinessState,
    pub hostname: Option<String>,
    pub interface_up: bool,
    pub dns_working: Option<bool>,
    pub outbound_udp: CapabilitySupport,
    pub outbound_srt: CapabilitySupport,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackupReadiness {
    pub state: ReadinessState,
    pub destination_configured: bool,
    pub writable: Option<bool>,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseReadiness {
    pub state: ReadinessState,
    pub candidate_verified: bool,
    pub update_available: bool,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessState {
    Pass,
    Blocked,
    Recoverable,
    Optional,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapabilitySupport {
    Supported,
    Unsupported,
    Unimplemented,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClockSyncStatus {
    Synchronized,
    Unsynchronized,
    Unknown,
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineTierVerdict {
    Base,
    IpBroadcast,
    FullBroadcast,
    Blocked,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceStationTier {
    Tier0BatchStream,
    Tier1Streaming,
    DevelopmentValidation,
    CableSdi,
    Tier2MultiStream,
}

/// The operator-selected target for this station, retained separately from the
/// measured [`ReferenceStationCatalog`].  The catalog answers what the current
/// probe can substantiate; this record answers which documented reference build
/// the station is being commissioned to satisfy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceStationTierSelection {
    pub catalog_version: u32,
    pub tier: ReferenceStationTier,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

/// An immutable audit row for an operator change to the selected reference
/// station tier.  The selected tier and catalog version are copied into every
/// row so later catalog revisions cannot rewrite historical intent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceStationTierAuditEvent {
    pub revision: u64,
    pub catalog_version: u32,
    pub tier: ReferenceStationTier,
    pub actor: String,
    pub occurred_at_unix_ms: i64,
}

impl ReferenceStationTier {
    /// Stable SQLite name. Do not derive this from `Debug` or serde output.
    pub const fn persisted_name(&self) -> &'static str {
        match self {
            Self::Tier0BatchStream => "tier_0_batch_stream",
            Self::Tier1Streaming => "tier_1_streaming",
            Self::DevelopmentValidation => "development_validation",
            Self::CableSdi => "cable_sdi",
            Self::Tier2MultiStream => "tier_2_multi_stream",
        }
    }

    /// Parses only names this release can safely interpret from persistent
    /// storage. Unknown names must fail closed rather than being coerced.
    pub fn parse_persisted_name(value: &str) -> Result<Self, String> {
        match value {
            "tier_0_batch_stream" => Ok(Self::Tier0BatchStream),
            "tier_1_streaming" => Ok(Self::Tier1Streaming),
            "development_validation" => Ok(Self::DevelopmentValidation),
            "cable_sdi" => Ok(Self::CableSdi),
            "tier_2_multi_stream" => Ok(Self::Tier2MultiStream),
            _ => Err(format!(
                "unknown persisted reference station tier {value:?}"
            )),
        }
    }
}

impl ReferenceStationTierSelection {
    pub fn validate(&self) -> Result<(), String> {
        if self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
            || !valid_reference_station_actor(&self.updated_by)
        {
            return Err("reference station tier selection facts are invalid".into());
        }
        Ok(())
    }

    pub const fn uses_current_catalog(&self) -> bool {
        self.catalog_version == REFERENCE_STATION_CATALOG_VERSION
    }
}

impl ReferenceStationTierAuditEvent {
    pub fn validate(&self) -> Result<(), String> {
        if self.revision == 0
            || self.occurred_at_unix_ms < 0
            || !valid_reference_station_actor(&self.actor)
        {
            return Err("reference station tier audit facts are invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceStationRequirementName {
    LogicalCpuCapacity,
    MemoryCapacity,
    MediaStorageCapacity,
    RedundantMediaStorage,
    MultiDiskZfsStorage,
    NetworkAttachedStorage,
    Gpu,
    OpenGl45,
    HardwareEncoder,
    Ups,
    IpOutput,
    SrtOutput,
    HlsOutput,
    SdiOutput,
    InterlacedOutput,
    DeckLinkHardware,
    BlackmagicDesktopVideo,
    NativeOperatingSystem,
    HeadendInterface,
    MultiChannelOutput,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceStationRequirementAssessment {
    pub name: ReferenceStationRequirementName,
    pub state: ReadinessState,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceStationTierAssessment {
    pub tier: ReferenceStationTier,
    pub state: ReadinessState,
    pub requirements: Vec<ReferenceStationRequirementAssessment>,
    pub detail: String,
    pub next_step: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReferenceStationCatalog {
    pub version: u32,
    pub matching_tiers: Vec<ReferenceStationTier>,
    pub assessments: Vec<ReferenceStationTierAssessment>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessDimensionName {
    StationIdentity,
    Hardware,
    ReferenceStation,
    Engine,
    OutputCapabilities,
    Clock,
    Network,
    Backup,
    Release,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadinessDimension {
    pub name: ReadinessDimensionName,
    pub state: ReadinessState,
    pub required: bool,
    pub detail: String,
    pub evidence: String,
    pub next_step: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PegReadinessColor {
    Green,
    Yellow,
    Red,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PegReadinessRollup {
    pub color: PegReadinessColor,
    pub dimensions: Vec<ReadinessDimension>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ValidationError {
    InvalidInput(String),
    StaleProbe { age_ms: i64, max_age_ms: i64 },
}

impl std::fmt::Display for ValidationError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidInput(message) => formatter.write_str(message),
            Self::StaleProbe { age_ms, max_age_ms } => {
                write!(
                    formatter,
                    "probe is stale: age {age_ms}ms exceeds {max_age_ms}ms"
                )
            }
        }
    }
}

impl std::error::Error for ValidationError {}

pub fn derive_station_box_profile(
    input: StationBoxProbeInput,
) -> Result<StationBoxProfile, ValidationError> {
    input.validate()?;
    let missing_factories = missing_factories(
        &input.engine.required_factories,
        &input.engine.available_factories,
    );
    let engine_state = if input.engine.playout == ReadinessState::Blocked
        || input.engine.runtime == ReadinessState::Blocked
        || input.engine.output == ReadinessState::Blocked
        || input.engine.gstreamer_version.is_none()
        || !missing_factories.is_empty()
    {
        ReadinessState::Blocked
    } else if [
        &input.engine.playout,
        &input.engine.runtime,
        &input.engine.output,
    ]
    .iter()
    .any(|state| matches!(state, ReadinessState::Recoverable | ReadinessState::Unknown))
    {
        ReadinessState::Recoverable
    } else {
        ReadinessState::Pass
    };
    let clock_state = match input.clock.sync_status {
        ClockSyncStatus::Synchronized if input.clock.configured_timezone.is_some() => {
            ReadinessState::Pass
        }
        ClockSyncStatus::Synchronized => ReadinessState::Blocked,
        ClockSyncStatus::Unsynchronized | ClockSyncStatus::Unavailable => ReadinessState::Blocked,
        ClockSyncStatus::Unknown => ReadinessState::Unknown,
    };
    let hardware_state = if input.hardware.media_disk_free_gb < 20.0 {
        ReadinessState::Blocked
    } else if input.hardware.media_disk_free_gb < 100.0 || input.hardware.ram_total_gb < 8.0 {
        ReadinessState::Recoverable
    } else {
        ReadinessState::Pass
    };
    let hardware = HardwareReadiness {
        logical_cpus: input.hardware.logical_cpus,
        ram_total_gb: input.hardware.ram_total_gb,
        media_disk_free_gb: input.hardware.media_disk_free_gb,
        state: hardware_state.clone(),
    };
    let engine = EngineReadiness {
        gstreamer_version: input.engine.gstreamer_version.clone(),
        required_factories: input.engine.required_factories.clone(),
        missing_factories,
        available_encoders: input.engine.available_encoders.clone(),
        udp: input.engine.udp.clone(),
        srt: input.engine.srt.clone(),
        sdi: input.engine.sdi.clone(),
        ndi: input.engine.ndi.clone(),
        interlaced: input.engine.interlaced.clone(),
        playout: input.engine.playout.clone(),
        runtime: input.engine.runtime.clone(),
        output: input.engine.output.clone(),
        state: engine_state.clone(),
    };
    let network = NetworkReadiness {
        state: input.network.state.clone(),
        hostname: input.network.hostname.clone(),
        interface_up: input.network.interface_up,
        dns_working: input.network.dns_working,
        outbound_udp: input.network.outbound_udp.clone(),
        outbound_srt: input.network.outbound_srt.clone(),
    };
    let backup = BackupReadiness {
        state: input.backup.state.clone(),
        destination_configured: input.backup.destination_configured,
        writable: input.backup.writable,
        detail: input.backup.detail.clone(),
    };
    let release = ReleaseReadiness {
        state: input.release.state.clone(),
        candidate_verified: input.release.candidate_verified,
        update_available: input.release.update_available,
        detail: input.release.detail.clone(),
    };
    let station_state =
        if input.station_id.is_some() && input.station_name.is_some() && input.timezone.is_some() {
            ReadinessState::Pass
        } else {
            ReadinessState::Blocked
        };
    let output_state = if input.engine.udp != CapabilitySupport::Supported {
        ReadinessState::Blocked
    } else if [
        &input.engine.srt,
        &input.engine.sdi,
        &input.engine.ndi,
        &input.engine.interlaced,
    ]
    .iter()
    .all(|support| **support == CapabilitySupport::Supported)
    {
        ReadinessState::Pass
    } else {
        ReadinessState::Recoverable
    };
    let mut missing_outputs = Vec::new();
    if input.engine.srt != CapabilitySupport::Supported {
        missing_outputs.push("SRT");
    }
    if input.engine.interlaced != CapabilitySupport::Supported {
        missing_outputs.push("interlaced 1080i59.94 and 480i29.97");
    }
    if input.engine.sdi != CapabilitySupport::Supported {
        missing_outputs.push("SDI");
    }
    if input.engine.ndi != CapabilitySupport::Supported {
        missing_outputs.push("NDI");
    }
    let reference_station_catalog =
        derive_reference_station_catalog(&input.hardware, &input.engine);
    let reference_station_state = reference_station_readiness_state(&reference_station_catalog);
    let dimensions = vec![
        dimension(
            ReadinessDimensionName::StationIdentity,
            &station_state,
            true,
            if station_state == ReadinessState::Pass {
                "Station identity and IANA timezone are commissioned.".into()
            } else {
                "Station identity, display name, or IANA timezone is not commissioned.".into()
            },
            "current authoritative station profile",
            (station_state != ReadinessState::Pass)
                .then(|| "Complete the station identity step.".into()),
        ),
        dimension(
            ReadinessDimensionName::Hardware,
            &hardware.state,
            true,
            format!(
                "{} logical processors, {:.1} GB RAM, {:.1} GB media free.",
                hardware.logical_cpus, hardware.ram_total_gb, hardware.media_disk_free_gb
            ),
            "current Windows memory and volume probe",
            (hardware.state != ReadinessState::Pass)
                .then(|| "Provide at least 8 GB RAM and 100 GB free media storage.".into()),
        ),
        dimension(
            ReadinessDimensionName::ReferenceStation,
            &reference_station_state,
            true,
            if !reference_station_catalog.matching_tiers.is_empty() {
                format!(
                    "This machine matches reference builds {}, but no operator-selected deployment tier is commissioned.",
                    reference_station_catalog
                        .matching_tiers
                        .iter()
                        .map(reference_tier_label)
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            } else {
                "No complete reference-build match or selected deployment tier exists; missing physical evidence remains fail-closed."
                    .into()
            },
            "versioned reference-build matrix and current StationBox facts",
            (reference_station_state != ReadinessState::Pass).then(|| {
                "Select a deployment tier, resolve its blocked requirements, and collect authenticated evidence for every unknown required item."
                    .into()
            }),
        ),
        dimension(
            ReadinessDimensionName::Engine,
            &engine_state,
            true,
            if engine_state == ReadinessState::Pass {
                format!(
                    "GStreamer {} and every required base factory are available.",
                    engine.gstreamer_version.as_deref().unwrap_or("unknown")
                )
            } else {
                format!(
                    "The base playout engine is unavailable; missing factories: {}.",
                    if engine.missing_factories.is_empty() {
                        "runtime or output readiness".into()
                    } else {
                        engine.missing_factories.join(", ")
                    }
                )
            },
            "current installed-runtime factory probe",
            (engine_state != ReadinessState::Pass)
                .then(|| "Repair the private media runtime before enabling output.".into()),
        ),
        dimension(
            ReadinessDimensionName::OutputCapabilities,
            &output_state,
            true,
            if missing_outputs.is_empty() {
                "Progressive IP, SRT, interlaced cable, SDI, and NDI outputs are available.".into()
            } else {
                format!(
                    "Progressive HD IP is available; remaining required outputs: {}.",
                    missing_outputs.join(", ")
                )
            },
            "typed output catalog and installed sink factories",
            (!missing_outputs.is_empty())
                .then(|| "Implement and machine-prove every listed output capability.".into()),
        ),
        dimension(
            ReadinessDimensionName::Clock,
            &clock_state,
            true,
            match input.clock.sync_status {
                ClockSyncStatus::Synchronized => {
                    "Station timezone and time synchronization are verified.".into()
                }
                ClockSyncStatus::Unknown => {
                    "Station timezone is configured but time synchronization is unknown.".into()
                }
                _ => "Station time synchronization is unavailable or unsynchronized.".into(),
            },
            "configured station timezone and current operating-system clock probe",
            (clock_state != ReadinessState::Pass)
                .then(|| "Configure and verify Windows time synchronization.".into()),
        ),
        dimension(
            ReadinessDimensionName::Network,
            &network.state,
            true,
            if network.interface_up {
                "The host selected a non-loopback local address; outbound delivery is not yet verified."
                    .into()
            } else {
                "No non-loopback local route is available.".into()
            },
            "current operating-system route-selection probe",
            (network.state != ReadinessState::Pass)
                .then(|| "Verify outbound delivery to the configured headend receiver.".into()),
        ),
        dimension(
            ReadinessDimensionName::Backup,
            &backup.state,
            false,
            backup
                .detail
                .clone()
                .unwrap_or_else(|| "Backup readiness has no current evidence.".into()),
            "current synchronized backup write/read/delete probe",
            (backup.state != ReadinessState::Pass)
                .then(|| "Configure a writable backup destination.".into()),
        ),
        dimension(
            ReadinessDimensionName::Release,
            &release.state,
            false,
            release
                .detail
                .clone()
                .unwrap_or_else(|| "Installed release identity has no current evidence.".into()),
            "installed and active-slot receipts",
            (release.state != ReadinessState::Pass)
                .then(|| "Install a manifest-verified release candidate.".into()),
        ),
    ];
    let color = if dimensions
        .iter()
        .any(|dimension| dimension.required && dimension.state == ReadinessState::Blocked)
    {
        PegReadinessColor::Red
    } else if dimensions.iter().any(|dimension| {
        matches!(
            dimension.state,
            ReadinessState::Blocked
                | ReadinessState::Recoverable
                | ReadinessState::Optional
                | ReadinessState::Unknown
        )
    }) {
        PegReadinessColor::Yellow
    } else {
        PegReadinessColor::Green
    };
    let peg_readiness = PegReadinessRollup { color, dimensions };
    let engine_tier_verdict = if engine_state == ReadinessState::Blocked {
        EngineTierVerdict::Blocked
    } else if input.engine.udp == CapabilitySupport::Supported
        && input.engine.srt == CapabilitySupport::Supported
        && input.engine.sdi == CapabilitySupport::Supported
        && input.engine.ndi == CapabilitySupport::Supported
        && input.engine.interlaced == CapabilitySupport::Supported
    {
        EngineTierVerdict::FullBroadcast
    } else if input.engine.udp == CapabilitySupport::Supported
        && input.engine.srt == CapabilitySupport::Supported
    {
        EngineTierVerdict::IpBroadcast
    } else if input.engine.udp == CapabilitySupport::Supported {
        EngineTierVerdict::Base
    } else {
        EngineTierVerdict::Unknown
    };
    Ok(StationBoxProfile {
        schema_version: SCHEMA_VERSION,
        generated_at_unix_ms: input.generated_at_unix_ms,
        product_version: input.product_version,
        source_commit: input.source_commit,
        station_id: input.station_id,
        station_name: input.station_name,
        timezone: input.timezone,
        hardware,
        engine,
        clock: ClockReadiness {
            configured_timezone: input.clock.configured_timezone,
            utc_offset_seconds: input.clock.utc_offset_seconds,
            sync_status: input.clock.sync_status,
            state: clock_state,
        },
        network,
        backup,
        release,
        engine_tier_verdict,
        reference_station_catalog,
        peg_readiness,
    })
}

fn derive_reference_station_catalog(
    hardware: &HardwareProbeInput,
    engine: &EngineProbeInput,
) -> ReferenceStationCatalog {
    let cpu = |minimum| {
        numeric_requirement(
            ReferenceStationRequirementName::LogicalCpuCapacity,
            hardware.logical_cpus as f64,
            minimum,
            "logical processors",
        )
    };
    let ram = |minimum| {
        numeric_requirement(
            ReferenceStationRequirementName::MemoryCapacity,
            hardware.ram_total_gb,
            minimum,
            "GB RAM",
        )
    };
    let ip = || {
        capability_requirement(
            ReferenceStationRequirementName::IpOutput,
            &engine.udp,
            "UDP MPEG-TS output capability",
        )
    };
    let srt = || {
        capability_requirement(
            ReferenceStationRequirementName::SrtOutput,
            &engine.srt,
            "SRT output capability",
        )
    };
    let hls = || {
        unknown_reference_requirement(
            ReferenceStationRequirementName::HlsOutput,
            "HLS output capability is not collected by the current engine probe.",
        )
    };
    let raid1 = || {
        unknown_reference_requirement(
            ReferenceStationRequirementName::RedundantMediaStorage,
            "Total capacity and 2x4TB NVMe RAID1 redundancy are not collected.",
        )
    };
    let assessments = vec![
        reference_tier_assessment(
            ReferenceStationTier::Tier0BatchStream,
            vec![cpu(16.0), ram(32.0), raid1(), ip(), srt(), hls()],
        ),
        reference_tier_assessment(
            ReferenceStationTier::Tier1Streaming,
            vec![
                cpu(16.0),
                ram(32.0),
                raid1(),
                optional_reference_requirement(
                    ReferenceStationRequirementName::Gpu,
                    "The reference build lists an RTX 4060, but the base streaming engine permits CPU playout.",
                ),
                ip(),
                srt(),
                hls(),
            ],
        ),
        reference_tier_assessment(
            ReferenceStationTier::DevelopmentValidation,
            vec![
                cpu(16.0),
                ram(32.0),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::MediaStorageCapacity,
                    "The 2TB NVMe reference-dev capacity is not collected; free space is not total capacity.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::Gpu,
                    "RTX 5070 Ti-class GPU identity and video memory are not collected.",
                ),
                ip(),
                srt(),
            ],
        ),
        reference_tier_assessment(
            ReferenceStationTier::CableSdi,
            vec![
                cpu(16.0),
                ram(32.0),
                raid1(),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::NetworkAttachedStorage,
                    "The required NAS target is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::Gpu,
                    "RTX 4060-4070-class GPU identity and video memory are not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::OpenGl45,
                    "OpenGL 4.5 support is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::HardwareEncoder,
                    "A hardware encoder cannot be inferred from the generic encoder-factory list.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::DeckLinkHardware,
                    "DeckLink hardware presence is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::BlackmagicDesktopVideo,
                    "Blackmagic Desktop Video driver and SDK identity are not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::NativeOperatingSystem,
                    "Native-host PCIe suitability is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::Ups,
                    "UPS presence and health are not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::HeadendInterface,
                    "The cable operator's required headend interface is not configured or probed.",
                ),
                ip(),
                srt(),
                capability_requirement(
                    ReferenceStationRequirementName::SdiOutput,
                    &engine.sdi,
                    "SDI output capability",
                ),
                capability_requirement(
                    ReferenceStationRequirementName::InterlacedOutput,
                    &engine.interlaced,
                    "interlaced cable output capability",
                ),
            ],
        ),
        reference_tier_assessment(
            ReferenceStationTier::Tier2MultiStream,
            vec![
                cpu(32.0),
                ram(128.0),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::MultiDiskZfsStorage,
                    "The 8x4TB NVMe ZFS storage topology is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::Gpu,
                    "RTX 4070 Ti Super-class GPU identity and video memory are not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::HardwareEncoder,
                    "Multi-stream hardware-encoder capacity is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::MultiChannelOutput,
                    "Concurrent multi-channel output capacity has not been machine-proved.",
                ),
                ip(),
                srt(),
                hls(),
                optional_capability_requirement(
                    ReferenceStationRequirementName::SdiOutput,
                    &engine.sdi,
                    "SDI output is optional per Tier 2 channel",
                ),
            ],
        ),
    ];
    let matching_tiers = assessments
        .iter()
        .filter(|assessment| assessment.state == ReadinessState::Pass)
        .map(|assessment| assessment.tier.clone())
        .collect();
    ReferenceStationCatalog {
        version: REFERENCE_STATION_CATALOG_VERSION,
        matching_tiers,
        assessments,
    }
}

fn reference_station_readiness_state(catalog: &ReferenceStationCatalog) -> ReadinessState {
    if catalog
        .assessments
        .iter()
        .all(|assessment| assessment.state == ReadinessState::Blocked)
    {
        ReadinessState::Blocked
    } else {
        // A generic build-row match cannot satisfy deployment readiness. This
        // becomes Pass only when a later slice binds the evidence to a
        // persisted, operator-selected tier revision.
        ReadinessState::Unknown
    }
}

fn reference_tier_assessment(
    tier: ReferenceStationTier,
    requirements: Vec<ReferenceStationRequirementAssessment>,
) -> ReferenceStationTierAssessment {
    let state = if requirements
        .iter()
        .any(|requirement| requirement.state == ReadinessState::Blocked)
    {
        ReadinessState::Blocked
    } else if requirements
        .iter()
        .any(|requirement| requirement.state == ReadinessState::Unknown)
    {
        ReadinessState::Unknown
    } else {
        ReadinessState::Pass
    };
    let detail = match &state {
        ReadinessState::Pass => "All measured and recorded reference requirements passed.".into(),
        ReadinessState::Blocked => {
            "One or more measured reference requirements are below the tier minimum.".into()
        }
        _ => "Physical requirements without machine evidence remain unqualified.".into(),
    };
    let blocked = requirement_names(&requirements, ReadinessState::Blocked);
    let unknown = requirement_names(&requirements, ReadinessState::Unknown);
    let mut actions = Vec::new();
    if !blocked.is_empty() {
        actions.push(format!(
            "Resolve blocked requirements: {}.",
            blocked.join(", ")
        ));
    }
    if !unknown.is_empty() {
        actions.push(format!("Collect evidence for: {}.", unknown.join(", ")));
    }
    ReferenceStationTierAssessment {
        tier,
        state,
        requirements,
        detail,
        next_step: (!actions.is_empty()).then(|| actions.join(" ")),
    }
}

fn numeric_requirement(
    name: ReferenceStationRequirementName,
    observed: f64,
    minimum: f64,
    unit: &str,
) -> ReferenceStationRequirementAssessment {
    let state = if observed >= minimum {
        ReadinessState::Pass
    } else {
        ReadinessState::Blocked
    };
    ReferenceStationRequirementAssessment {
        name,
        state,
        detail: format!("{observed:.1} {unit} observed; minimum is {minimum:.1}."),
    }
}

fn capability_requirement(
    name: ReferenceStationRequirementName,
    support: &CapabilitySupport,
    label: &str,
) -> ReferenceStationRequirementAssessment {
    let state = match support {
        CapabilitySupport::Supported => ReadinessState::Pass,
        CapabilitySupport::Unsupported | CapabilitySupport::Unimplemented => {
            ReadinessState::Blocked
        }
        CapabilitySupport::Unknown => ReadinessState::Unknown,
    };
    ReferenceStationRequirementAssessment {
        name,
        state,
        detail: format!("{label} is {}.", capability_label(support)),
    }
}

fn unknown_reference_requirement(
    name: ReferenceStationRequirementName,
    detail: &str,
) -> ReferenceStationRequirementAssessment {
    ReferenceStationRequirementAssessment {
        name,
        state: ReadinessState::Unknown,
        detail: detail.into(),
    }
}

fn optional_reference_requirement(
    name: ReferenceStationRequirementName,
    detail: &str,
) -> ReferenceStationRequirementAssessment {
    ReferenceStationRequirementAssessment {
        name,
        state: ReadinessState::Optional,
        detail: detail.into(),
    }
}

fn optional_capability_requirement(
    name: ReferenceStationRequirementName,
    support: &CapabilitySupport,
    label: &str,
) -> ReferenceStationRequirementAssessment {
    ReferenceStationRequirementAssessment {
        name,
        state: ReadinessState::Optional,
        detail: format!(
            "{label}; current capability is {}.",
            capability_label(support)
        ),
    }
}

fn requirement_names(
    requirements: &[ReferenceStationRequirementAssessment],
    state: ReadinessState,
) -> Vec<&'static str> {
    requirements
        .iter()
        .filter(|requirement| requirement.state == state)
        .map(|requirement| reference_requirement_label(&requirement.name))
        .collect()
}

fn reference_tier_label(tier: &ReferenceStationTier) -> &'static str {
    match tier {
        ReferenceStationTier::Tier0BatchStream => "Tier 0 batch/stream",
        ReferenceStationTier::Tier1Streaming => "Tier 1 streaming",
        ReferenceStationTier::DevelopmentValidation => "reference development/validation",
        ReferenceStationTier::CableSdi => "Cable/SDI",
        ReferenceStationTier::Tier2MultiStream => "Tier 2 multi-stream",
    }
}

fn reference_requirement_label(requirement: &ReferenceStationRequirementName) -> &'static str {
    match requirement {
        ReferenceStationRequirementName::LogicalCpuCapacity => "CPU capacity",
        ReferenceStationRequirementName::MemoryCapacity => "memory capacity",
        ReferenceStationRequirementName::MediaStorageCapacity => "media storage capacity",
        ReferenceStationRequirementName::RedundantMediaStorage => "redundant media storage",
        ReferenceStationRequirementName::MultiDiskZfsStorage => "multi-disk ZFS storage",
        ReferenceStationRequirementName::NetworkAttachedStorage => "network-attached storage",
        ReferenceStationRequirementName::Gpu => "GPU",
        ReferenceStationRequirementName::OpenGl45 => "OpenGL 4.5",
        ReferenceStationRequirementName::HardwareEncoder => "hardware encoder",
        ReferenceStationRequirementName::Ups => "UPS",
        ReferenceStationRequirementName::IpOutput => "IP output",
        ReferenceStationRequirementName::SrtOutput => "SRT output",
        ReferenceStationRequirementName::HlsOutput => "HLS output",
        ReferenceStationRequirementName::SdiOutput => "SDI output",
        ReferenceStationRequirementName::InterlacedOutput => "interlaced output",
        ReferenceStationRequirementName::DeckLinkHardware => "DeckLink hardware",
        ReferenceStationRequirementName::BlackmagicDesktopVideo => "Blackmagic Desktop Video",
        ReferenceStationRequirementName::NativeOperatingSystem => "native operating system",
        ReferenceStationRequirementName::HeadendInterface => "headend interface",
        ReferenceStationRequirementName::MultiChannelOutput => "multi-channel output",
    }
}

fn capability_label(support: &CapabilitySupport) -> &'static str {
    match support {
        CapabilitySupport::Supported => "supported",
        CapabilitySupport::Unsupported => "unsupported",
        CapabilitySupport::Unimplemented => "unimplemented",
        CapabilitySupport::Unknown => "unknown",
    }
}

impl StationBoxProbeInput {
    pub fn validate(&self) -> Result<(), ValidationError> {
        if self.generated_at_unix_ms < 0 || self.probe_collected_at_unix_ms < 0 {
            return invalid("timestamps must be nonnegative");
        }
        if self.probe_collected_at_unix_ms > self.generated_at_unix_ms {
            return invalid("probe collection cannot be after profile generation");
        }
        if self.max_probe_age_ms <= 0 {
            return invalid("max_probe_age_ms must be positive");
        }
        let age_ms = self.generated_at_unix_ms - self.probe_collected_at_unix_ms;
        if age_ms > self.max_probe_age_ms {
            return Err(ValidationError::StaleProbe {
                age_ms,
                max_age_ms: self.max_probe_age_ms,
            });
        }
        visible(&self.product_version, "product_version", MAX_TEXT)?;
        if self
            .source_commit
            .as_deref()
            .is_some_and(|commit| !is_commit(commit))
        {
            return invalid("source_commit must be absent or a 40-character lowercase commit hash");
        }
        for (field, value) in [
            ("station_id", self.station_id.as_deref()),
            ("station_name", self.station_name.as_deref()),
            ("timezone", self.timezone.as_deref()),
        ] {
            if let Some(value) = value {
                visible(value, field, MAX_TEXT)?;
            }
        }
        if self
            .station_id
            .as_deref()
            .is_some_and(|value| !is_uuid(value))
        {
            return invalid("station_id must be a canonical UUID");
        }
        if self
            .timezone
            .as_deref()
            .is_some_and(|value| !is_iana_timezone(value))
        {
            return invalid("timezone must be an IANA timezone name");
        }
        validate_hardware(&self.hardware)?;
        validate_engine(&self.engine)?;
        validate_clock(&self.clock)?;
        validate_network(&self.network)?;
        validate_backup(&self.backup)?;
        validate_release(&self.release)?;
        if self.release.candidate_verified && self.source_commit.is_none() {
            return invalid("a verified release requires a source commit identity");
        }
        Ok(())
    }
}

fn validate_hardware(input: &HardwareProbeInput) -> Result<(), ValidationError> {
    if input.logical_cpus == 0
        || !input.ram_total_gb.is_finite()
        || input.ram_total_gb <= 0.0
        || !input.media_disk_free_gb.is_finite()
        || input.media_disk_free_gb < 0.0
    {
        return invalid("hardware measurements are impossible or nonfinite");
    }
    Ok(())
}

fn validate_engine(input: &EngineProbeInput) -> Result<(), ValidationError> {
    if input.required_factories.is_empty() {
        return invalid("required_factories must not be empty");
    }
    validate_names(&input.required_factories, "required_factories")?;
    validate_names(&input.available_factories, "available_factories")?;
    validate_names(&input.available_encoders, "available_encoders")?;
    if has_duplicates(&input.required_factories)
        || has_duplicates(&input.available_factories)
        || has_duplicates(&input.available_encoders)
    {
        return invalid("engine factory and encoder lists must not contain duplicates");
    }
    if input
        .gstreamer_version
        .as_deref()
        .is_some_and(|version| visible(version, "gstreamer_version", MAX_TEXT).is_err())
    {
        return invalid("gstreamer_version is malformed");
    }
    Ok(())
}

fn validate_clock(input: &ClockProbeInput) -> Result<(), ValidationError> {
    if input
        .configured_timezone
        .as_deref()
        .is_some_and(|timezone| {
            visible(timezone, "configured_timezone", MAX_TEXT).is_err()
                || !is_iana_timezone(timezone)
        })
        || input
            .utc_offset_seconds
            .is_some_and(|offset| !(-50_400..=50_400).contains(&offset))
    {
        return invalid("clock configuration is malformed");
    }
    Ok(())
}

fn validate_network(input: &NetworkProbeInput) -> Result<(), ValidationError> {
    if input
        .hostname
        .as_deref()
        .is_some_and(|hostname| visible(hostname, "network.hostname", MAX_TEXT).is_err())
    {
        return invalid("network hostname is malformed");
    }
    if input.state == ReadinessState::Pass
        && (!input.interface_up || input.outbound_udp != CapabilitySupport::Supported)
    {
        return invalid("network cannot pass without an interface and verified outbound UDP");
    }
    Ok(())
}

fn validate_backup(input: &BackupProbeInput) -> Result<(), ValidationError> {
    if input
        .detail
        .as_deref()
        .is_some_and(|detail| visible(detail, "backup.detail", MAX_TEXT).is_err())
    {
        return invalid("backup detail is malformed");
    }
    if input.state == ReadinessState::Pass
        && (!input.destination_configured || input.writable != Some(true))
    {
        return invalid("backup cannot pass without a configured writable destination");
    }
    Ok(())
}

fn validate_release(input: &ReleaseProbeInput) -> Result<(), ValidationError> {
    if input
        .detail
        .as_deref()
        .is_some_and(|detail| visible(detail, "release.detail", MAX_TEXT).is_err())
    {
        return invalid("release detail is malformed");
    }
    if input.state == ReadinessState::Pass && !input.candidate_verified {
        return invalid("release cannot pass without candidate verification");
    }
    Ok(())
}

fn validate_names(values: &[String], field: &str) -> Result<(), ValidationError> {
    if values.len() > MAX_LIST_ITEMS
        || values
            .iter()
            .any(|value| visible(value, field, MAX_FACTORY_NAME).is_err())
    {
        return invalid(format!("{field} contains malformed or too many names"));
    }
    Ok(())
}

fn missing_factories(required: &[String], available: &[String]) -> Vec<String> {
    let mut missing: Vec<String> = required
        .iter()
        .filter(|factory| !available.iter().any(|candidate| candidate == *factory))
        .cloned()
        .collect();
    missing.sort();
    missing.dedup();
    missing
}

fn dimension(
    name: ReadinessDimensionName,
    state: &ReadinessState,
    required: bool,
    detail: String,
    evidence: &str,
    next_step: Option<String>,
) -> ReadinessDimension {
    ReadinessDimension {
        name,
        state: state.clone(),
        required,
        detail,
        evidence: evidence.into(),
        next_step,
    }
}

fn has_duplicates(values: &[String]) -> bool {
    let mut sorted = values.to_vec();
    sorted.sort();
    sorted.windows(2).any(|pair| pair[0] == pair[1])
}

fn visible(value: &str, field: &str, maximum: usize) -> Result<(), ValidationError> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return invalid(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

fn is_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|index| bytes[*index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            [8, 13, 18, 23].contains(&index)
                || byte.is_ascii_digit()
                || (b'a'..=b'f').contains(byte)
        })
}

fn is_iana_timezone(value: &str) -> bool {
    value == "UTC"
        || (value.contains('/')
            && value.split('/').all(|segment| {
                !segment.is_empty()
                    && segment.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'+')
                    })
            }))
}

fn valid_reference_station_actor(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_TEXT
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

fn invalid(message: impl Into<String>) -> Result<(), ValidationError> {
    Err(ValidationError::InvalidInput(message.into()))
}

fn default_max_probe_age_ms() -> i64 {
    DEFAULT_MAX_PROBE_AGE_MS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generic_tier_match_cannot_replace_selected_deployment_intent() {
        let catalog = ReferenceStationCatalog {
            version: REFERENCE_STATION_CATALOG_VERSION,
            matching_tiers: vec![ReferenceStationTier::Tier0BatchStream],
            assessments: vec![ReferenceStationTierAssessment {
                tier: ReferenceStationTier::Tier0BatchStream,
                state: ReadinessState::Pass,
                requirements: Vec::new(),
                detail: "generic match".into(),
                next_step: None,
            }],
        };

        assert_eq!(
            reference_station_readiness_state(&catalog),
            ReadinessState::Unknown
        );
    }

    #[test]
    fn reference_tier_persisted_names_are_stable_and_unknown_names_fail_closed() {
        for tier in [
            ReferenceStationTier::Tier0BatchStream,
            ReferenceStationTier::Tier1Streaming,
            ReferenceStationTier::DevelopmentValidation,
            ReferenceStationTier::CableSdi,
            ReferenceStationTier::Tier2MultiStream,
        ] {
            assert_eq!(
                ReferenceStationTier::parse_persisted_name(tier.persisted_name()).unwrap(),
                tier
            );
        }
        assert!(ReferenceStationTier::parse_persisted_name("future_tier").is_err());
    }

    #[test]
    fn persisted_selection_keeps_old_catalog_version_visible_but_not_current() {
        let selection = ReferenceStationTierSelection {
            catalog_version: REFERENCE_STATION_CATALOG_VERSION - 1,
            tier: ReferenceStationTier::Tier1Streaming,
            revision: 1,
            created_at_unix_ms: 100,
            updated_at_unix_ms: 100,
            updated_by: "operator-1".into(),
        };
        selection.validate().unwrap();
        assert!(!selection.uses_current_catalog());
    }
}
