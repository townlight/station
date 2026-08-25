//! Pure, deterministic Station Box readiness contracts.
//!
//! Platform-specific probing is intentionally outside this crate. Callers build a
//! [`StationBoxProbeInput`] from installed-machine evidence and pass it to
//! [`derive_station_box_profile`].

use serde::{Deserialize, Serialize};

mod headend;

pub use headend::*;

const SCHEMA_VERSION: u32 = 6;
const MAX_TEXT: usize = 200;
const MAX_FACTORY_NAME: usize = 120;
const MAX_LIST_ITEMS: usize = 256;
const MAX_MACHINE_INVENTORY_ITEMS: usize = 64;
const MAX_MACHINE_GIB: f64 = 1_048_576.0;
const DEFAULT_MAX_PROBE_AGE_MS: i64 = 300_000;
pub const REFERENCE_STATION_CATALOG_VERSION: u32 = 2;

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
    #[serde(default)]
    pub reference_station_tier_selection: Option<ReferenceStationTierSelection>,
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
    pub machine_inventory: MachineInventoryEvidence,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MachineInventoryCollectionState {
    Collected,
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MachineEvidenceSource {
    WindowsCim,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MachineInventoryEvidence {
    pub collected_at_unix_ms: i64,
    pub source: MachineEvidenceSource,
    pub gpu_collection: MachineInventoryCollectionState,
    pub storage_collection: MachineInventoryCollectionState,
    pub device_collection: MachineInventoryCollectionState,
    pub media_volume_total_gb: Option<f64>,
    pub media_volume_backing_disks: Vec<MediaVolumeBackingDiskEvidence>,
    pub media_volume_backing_disks_verified: bool,
    pub gpu_adapters: Vec<GpuAdapterEvidence>,
    pub physical_disks: Vec<PhysicalDiskEvidence>,
    pub virtual_disks: Vec<VirtualDiskEvidence>,
    pub ups_devices: Vec<UpsDeviceEvidence>,
    pub decklink_devices: Vec<DeckLinkDeviceEvidence>,
    pub blackmagic_drivers: Vec<BlackmagicDriverEvidence>,
}

/// A same-snapshot binding from the configured media volume to the disks
/// reported by Windows for that volume. This is intentionally separate from
/// the aggregate disk inventory: aggregate NVMe or mirror-like observations
/// cannot qualify the media volume without this relation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MediaVolumeBackingDiskEvidence {
    pub name: String,
    pub size_gb: f64,
    pub media_type: Option<String>,
    pub bus_type: Option<String>,
    pub health_status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GpuAdapterEvidence {
    pub name: String,
    pub adapter_memory_gb: Option<f64>,
    pub driver_version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PhysicalDiskEvidence {
    pub name: String,
    pub size_gb: f64,
    pub media_type: Option<String>,
    pub bus_type: Option<String>,
    pub health_status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VirtualDiskEvidence {
    pub name: String,
    pub size_gb: f64,
    pub resiliency: Option<String>,
    pub health_status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpsDeviceEvidence {
    pub name: String,
    pub manufacturer: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeckLinkDeviceEvidence {
    pub name: String,
    pub manufacturer: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlackmagicDriverEvidence {
    pub device_name: String,
    pub manufacturer: Option<String>,
    pub driver_version: Option<String>,
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
    #[serde(default)]
    pub reference_station_tier_selection: Option<ReferenceStationTierSelection>,
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
    pub machine_inventory: MachineInventoryEvidence,
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
    MediaStorageMedium,
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
        machine_inventory: input.hardware.machine_inventory.clone(),
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
    let reference_station_selection = input.reference_station_tier_selection.as_ref();
    let reference_station_state =
        reference_station_readiness_state(&reference_station_catalog, reference_station_selection);
    let reference_station_detail =
        reference_station_dimension_detail(&reference_station_catalog, reference_station_selection);
    let reference_station_next_step = reference_station_dimension_next_step(
        &reference_station_catalog,
        reference_station_selection,
        &reference_station_state,
    );
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
            reference_station_detail,
            "versioned reference-build matrix and current StationBox facts",
            reference_station_next_step,
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
            "installed, active-slot, and runtime-manifest evidence",
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
        reference_station_tier_selection: input.reference_station_tier_selection,
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
            if hardware.machine_inventory.storage_collection
                == MachineInventoryCollectionState::Collected
                && hardware.machine_inventory.virtual_disks.iter().any(|disk| {
                    disk.resiliency
                        .as_deref()
                        .is_some_and(|value| value.eq_ignore_ascii_case("mirror"))
                })
            {
                "Mirror-like virtual-disk topology was observed, but its linkage to the media volume and fault tolerance are unproven."
            } else {
                "Total capacity and 2x4TB NVMe RAID1 redundancy are not collected."
            },
        )
    };
    let media_storage_capacity = || match hardware.machine_inventory.media_volume_total_gb {
        Some(total)
            if hardware.machine_inventory.storage_collection
                == MachineInventoryCollectionState::Collected =>
        {
            numeric_requirement(
                ReferenceStationRequirementName::MediaStorageCapacity,
                total,
                1_800.0,
                "GiB total media storage (2 TB nominal class)",
            )
        }
        _ => unknown_reference_requirement(
            ReferenceStationRequirementName::MediaStorageCapacity,
            "The 2TB NVMe reference-dev capacity is not collected; free space is not total capacity.",
        ),
    };
    let media_storage_medium = || {
        let backing_disks = &hardware.machine_inventory.media_volume_backing_disks;
        let linked_nvme = hardware.machine_inventory.storage_collection
            == MachineInventoryCollectionState::Collected
            && hardware.machine_inventory.media_volume_total_gb.is_some()
            && hardware
                .machine_inventory
                .media_volume_backing_disks_verified
            && !backing_disks.is_empty()
            && backing_disks.iter().all(|disk| {
                disk.bus_type
                    .as_deref()
                    .is_some_and(|value| value.eq_ignore_ascii_case("nvme"))
            });
        if linked_nvme {
            passing_reference_requirement(
                ReferenceStationRequirementName::MediaStorageMedium,
                format!(
                    "The configured media volume is linked to {} NVMe backing disk(s) in the current Windows storage snapshot.",
                    backing_disks.len()
                ),
            )
        } else {
            let detail = if hardware.machine_inventory.storage_collection
                == MachineInventoryCollectionState::Collected
                && hardware
                    .machine_inventory
                    .physical_disks
                    .iter()
                    .any(|disk| {
                        disk.bus_type
                            .as_deref()
                            .is_some_and(|value| value.eq_ignore_ascii_case("nvme"))
                    }) {
                "NVMe physical storage was observed, but its linkage to the media volume is not authenticated."
            } else {
                "The required NVMe medium for the media volume is not authenticated."
            };
            unknown_reference_requirement(
                ReferenceStationRequirementName::MediaStorageMedium,
                detail,
            )
        }
    };
    let gpu = || {
        let detail = if hardware.machine_inventory.gpu_collection
            == MachineInventoryCollectionState::Collected
            && !hardware.machine_inventory.gpu_adapters.is_empty()
        {
            format!(
                "Observed GPU adapters: {}; GPU class and throughput are not inferred.",
                hardware
                    .machine_inventory
                    .gpu_adapters
                    .iter()
                    .map(|adapter| adapter.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        } else {
            "GPU identity and video memory are not collected.".into()
        };
        unknown_reference_requirement(ReferenceStationRequirementName::Gpu, &detail)
    };
    let decklink = || {
        let detail = if hardware.machine_inventory.device_collection
            == MachineInventoryCollectionState::Collected
            && !hardware.machine_inventory.decklink_devices.is_empty()
        {
            format!(
                "DeckLink-named PnP devices observed: {}; stable hardware identity and function are not authenticated.",
                hardware
                    .machine_inventory
                    .decklink_devices
                    .iter()
                    .map(|device| device.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        } else {
            "DeckLink hardware identity and function are not authenticated.".into()
        };
        unknown_reference_requirement(ReferenceStationRequirementName::DeckLinkHardware, &detail)
    };
    let blackmagic_driver = || {
        let detail = if hardware.machine_inventory.device_collection
            == MachineInventoryCollectionState::Collected
            && !hardware.machine_inventory.blackmagic_drivers.is_empty()
        {
            format!(
                "Blackmagic drivers observed: {}; SDK/runtime compatibility is not inferred.",
                hardware
                    .machine_inventory
                    .blackmagic_drivers
                    .iter()
                    .map(|driver| driver.device_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        } else {
            "Blackmagic Desktop Video driver and SDK identity are not collected.".into()
        };
        unknown_reference_requirement(
            ReferenceStationRequirementName::BlackmagicDesktopVideo,
            &detail,
        )
    };
    let ups = || {
        let detail = if hardware.machine_inventory.device_collection
            == MachineInventoryCollectionState::Collected
            && !hardware.machine_inventory.ups_devices.is_empty()
        {
            format!(
                "UPS devices observed: {}; UPS health and power protection are not inferred.",
                hardware
                    .machine_inventory
                    .ups_devices
                    .iter()
                    .map(|device| device.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        } else {
            "UPS presence and health are not collected.".into()
        };
        unknown_reference_requirement(ReferenceStationRequirementName::Ups, &detail)
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
                media_storage_capacity(),
                media_storage_medium(),
                gpu(),
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
                gpu(),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::OpenGl45,
                    "OpenGL 4.5 support is not collected.",
                ),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::HardwareEncoder,
                    "A hardware encoder cannot be inferred from the generic encoder-factory list.",
                ),
                decklink(),
                blackmagic_driver(),
                unknown_reference_requirement(
                    ReferenceStationRequirementName::NativeOperatingSystem,
                    "Native-host PCIe suitability is not collected.",
                ),
                ups(),
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
                gpu(),
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

fn reference_station_readiness_state(
    catalog: &ReferenceStationCatalog,
    selection: Option<&ReferenceStationTierSelection>,
) -> ReadinessState {
    let Some(selection) = selection else {
        return ReadinessState::Unknown;
    };
    if !selection.uses_current_catalog() || catalog.version != selection.catalog_version {
        return ReadinessState::Unknown;
    }
    catalog
        .assessments
        .iter()
        .find(|assessment| assessment.tier == selection.tier)
        .map(|assessment| match assessment.state {
            ReadinessState::Pass => ReadinessState::Pass,
            ReadinessState::Blocked => ReadinessState::Blocked,
            ReadinessState::Unknown => ReadinessState::Unknown,
            _ => ReadinessState::Unknown,
        })
        .unwrap_or(ReadinessState::Unknown)
}

fn reference_station_dimension_detail(
    catalog: &ReferenceStationCatalog,
    selection: Option<&ReferenceStationTierSelection>,
) -> String {
    let Some(selection) = selection else {
        return if !catalog.matching_tiers.is_empty() {
            format!(
                "This machine matches reference builds {}, but no operator-selected deployment tier is commissioned.",
                catalog
                    .matching_tiers
                    .iter()
                    .map(reference_tier_label)
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        } else {
            "No complete reference-build match or selected deployment tier exists; missing physical evidence remains fail-closed."
                .into()
        };
    };
    if !selection.uses_current_catalog() || catalog.version != selection.catalog_version {
        return format!(
            "Selected {} uses reference catalog version {}, while the installed catalog is version {}; readiness is unknown until reselected.",
            reference_tier_label(&selection.tier),
            selection.catalog_version,
            catalog.version,
        );
    }
    catalog
        .assessments
        .iter()
        .find(|assessment| assessment.tier == selection.tier)
        .map(|assessment| {
            format!(
                "Selected {} (revision {}) is assessed as {}: {}",
                reference_tier_label(&selection.tier),
                selection.revision,
                readiness_state_label(&assessment.state),
                assessment.detail
            )
        })
        .unwrap_or_else(|| {
            format!(
                "Selected {} is not present in the installed reference catalog; readiness is unknown.",
                reference_tier_label(&selection.tier)
            )
        })
}

fn reference_station_dimension_next_step(
    catalog: &ReferenceStationCatalog,
    selection: Option<&ReferenceStationTierSelection>,
    state: &ReadinessState,
) -> Option<String> {
    if *state == ReadinessState::Pass {
        return None;
    }
    match selection {
        Some(selection)
            if selection.uses_current_catalog()
                && selection.catalog_version == catalog.version
                && catalog
                    .assessments
                    .iter()
                    .any(|assessment| assessment.tier == selection.tier) =>
        {
            Some(
                "Resolve the selected tier's blocked requirements and collect authenticated evidence for every unknown required item."
                    .into(),
            )
        }
        _ => Some(
            "Select a deployment tier from the current reference catalog before collecting its missing evidence."
                .into(),
        ),
    }
}

fn readiness_state_label(state: &ReadinessState) -> &'static str {
    match state {
        ReadinessState::Pass => "pass",
        ReadinessState::Blocked => "blocked",
        ReadinessState::Recoverable => "recoverable",
        ReadinessState::Optional => "optional",
        ReadinessState::Unknown => "unknown",
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

fn passing_reference_requirement(
    name: ReferenceStationRequirementName,
    detail: String,
) -> ReferenceStationRequirementAssessment {
    ReferenceStationRequirementAssessment {
        name,
        state: ReadinessState::Pass,
        detail,
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
        ReferenceStationRequirementName::MediaStorageMedium => "media storage medium",
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
        validate_hardware(
            &self.hardware,
            self.probe_collected_at_unix_ms,
            self.generated_at_unix_ms,
        )?;
        validate_engine(&self.engine)?;
        validate_clock(&self.clock)?;
        validate_network(&self.network)?;
        validate_backup(&self.backup)?;
        validate_release(&self.release)?;
        if let Some(selection) = &self.reference_station_tier_selection {
            selection
                .validate()
                .map_err(ValidationError::InvalidInput)?;
            if selection.updated_at_unix_ms > self.generated_at_unix_ms {
                return invalid(
                    "reference station tier selection cannot be updated after profile generation",
                );
            }
        }
        if self.release.candidate_verified && self.source_commit.is_none() {
            return invalid("a verified release requires a source commit identity");
        }
        Ok(())
    }
}

fn validate_hardware(
    input: &HardwareProbeInput,
    probe_collected_at_unix_ms: i64,
    generated_at_unix_ms: i64,
) -> Result<(), ValidationError> {
    if input.logical_cpus == 0
        || !input.ram_total_gb.is_finite()
        || input.ram_total_gb <= 0.0
        || !input.media_disk_free_gb.is_finite()
        || input.media_disk_free_gb < 0.0
    {
        return invalid("hardware measurements are impossible or nonfinite");
    }
    validate_machine_inventory(
        &input.machine_inventory,
        input.media_disk_free_gb,
        probe_collected_at_unix_ms,
        generated_at_unix_ms,
    )?;
    Ok(())
}

fn validate_machine_inventory(
    input: &MachineInventoryEvidence,
    media_disk_free_gb: f64,
    probe_collected_at_unix_ms: i64,
    generated_at_unix_ms: i64,
) -> Result<(), ValidationError> {
    if input.collected_at_unix_ms < probe_collected_at_unix_ms
        || input.collected_at_unix_ms > generated_at_unix_ms
    {
        return invalid("machine inventory timestamp is stale or outside probe generation");
    }
    validate_inventory_group(&input.gpu_collection, input.gpu_adapters.is_empty(), "GPU")?;
    validate_inventory_group(
        &input.storage_collection,
        input.media_volume_total_gb.is_none()
            && input.media_volume_backing_disks.is_empty()
            && input.physical_disks.is_empty()
            && input.virtual_disks.is_empty(),
        "storage",
    )?;
    validate_inventory_group(
        &input.device_collection,
        input.ups_devices.is_empty()
            && input.decklink_devices.is_empty()
            && input.blackmagic_drivers.is_empty(),
        "device",
    )?;
    if input
        .media_volume_total_gb
        .is_some_and(|value| !plausible_gib(value) || value < media_disk_free_gb)
    {
        return invalid("machine inventory media volume total is impossible");
    }
    validate_inventory_list(&input.gpu_adapters, "GPU adapters")?;
    validate_inventory_list(
        &input.media_volume_backing_disks,
        "media-volume backing disks",
    )?;
    if input.media_volume_backing_disks_verified != !input.media_volume_backing_disks.is_empty() {
        return invalid("media-volume backing-disk verification does not match its facts");
    }
    validate_inventory_list(&input.physical_disks, "physical disks")?;
    validate_inventory_list(&input.virtual_disks, "virtual disks")?;
    validate_inventory_list(&input.ups_devices, "UPS devices")?;
    validate_inventory_list(&input.decklink_devices, "DeckLink devices")?;
    validate_inventory_list(&input.blackmagic_drivers, "Blackmagic drivers")?;
    for adapter in &input.gpu_adapters {
        visible(&adapter.name, "GPU adapter name", MAX_TEXT)?;
        optional_visible(
            adapter.driver_version.as_deref(),
            "GPU adapter driver version",
        )?;
        if adapter
            .adapter_memory_gb
            .is_some_and(|value| !plausible_gib(value))
        {
            return invalid("GPU adapter memory is impossible");
        }
    }
    for disk in &input.physical_disks {
        validate_disk_text(
            &disk.name,
            disk.size_gb,
            &disk.media_type,
            &disk.bus_type,
            &disk.health_status,
            "physical disk",
        )?;
    }
    for disk in &input.media_volume_backing_disks {
        validate_disk_text(
            &disk.name,
            disk.size_gb,
            &disk.media_type,
            &disk.bus_type,
            &disk.health_status,
            "media-volume backing disk",
        )?;
    }
    if input.media_volume_backing_disks_verified {
        let Some(total_gb) = input.media_volume_total_gb else {
            return invalid("verified media-volume backing disks require total capacity");
        };
        let backing_total_gb = input
            .media_volume_backing_disks
            .iter()
            .map(|disk| disk.size_gb)
            .sum::<f64>();
        if !backing_total_gb.is_finite() || backing_total_gb < total_gb {
            return invalid("media-volume backing disks do not cover total capacity");
        }
    }
    for disk in &input.virtual_disks {
        visible(&disk.name, "virtual disk name", MAX_TEXT)?;
        if !plausible_gib(disk.size_gb) {
            return invalid("virtual disk size is impossible");
        }
        optional_visible(disk.resiliency.as_deref(), "virtual disk resiliency")?;
        optional_visible(disk.health_status.as_deref(), "virtual disk health status")?;
    }
    for device in &input.ups_devices {
        visible(&device.name, "UPS device name", MAX_TEXT)?;
        optional_visible(device.manufacturer.as_deref(), "UPS device manufacturer")?;
    }
    for device in &input.decklink_devices {
        visible(&device.name, "DeckLink device name", MAX_TEXT)?;
        optional_visible(
            device.manufacturer.as_deref(),
            "DeckLink device manufacturer",
        )?;
    }
    for driver in &input.blackmagic_drivers {
        visible(
            &driver.device_name,
            "Blackmagic driver device name",
            MAX_TEXT,
        )?;
        optional_visible(
            driver.manufacturer.as_deref(),
            "Blackmagic driver manufacturer",
        )?;
        optional_visible(
            driver.driver_version.as_deref(),
            "Blackmagic driver version",
        )?;
    }
    Ok(())
}

impl MachineInventoryEvidence {
    pub fn validate_for_probe(
        &self,
        media_disk_free_gb: f64,
        probe_collected_at_unix_ms: i64,
        generated_at_unix_ms: i64,
    ) -> Result<(), ValidationError> {
        validate_machine_inventory(
            self,
            media_disk_free_gb,
            probe_collected_at_unix_ms,
            generated_at_unix_ms,
        )
    }
}

fn validate_inventory_group(
    state: &MachineInventoryCollectionState,
    has_no_facts: bool,
    name: &str,
) -> Result<(), ValidationError> {
    if *state == MachineInventoryCollectionState::Unavailable && !has_no_facts {
        return invalid(format!("unavailable {name} inventory cannot carry facts"));
    }
    Ok(())
}

fn validate_inventory_list<T: PartialEq>(values: &[T], name: &str) -> Result<(), ValidationError> {
    if values.len() > MAX_MACHINE_INVENTORY_ITEMS || has_exact_duplicates(values) {
        return invalid(format!("{name} contains too many or duplicate facts"));
    }
    Ok(())
}

fn has_exact_duplicates<T: PartialEq>(values: &[T]) -> bool {
    values
        .iter()
        .enumerate()
        .any(|(index, value)| values[..index].iter().any(|prior| prior == value))
}

fn plausible_gib(value: f64) -> bool {
    value.is_finite() && value > 0.0 && value <= MAX_MACHINE_GIB
}

fn optional_visible(value: Option<&str>, field: &str) -> Result<(), ValidationError> {
    if value.is_some_and(|value| visible(value, field, MAX_TEXT).is_err()) {
        return invalid(format!("{field} is malformed"));
    }
    Ok(())
}

fn validate_disk_text(
    name: &str,
    size_gb: f64,
    media_type: &Option<String>,
    bus_type: &Option<String>,
    health_status: &Option<String>,
    kind: &str,
) -> Result<(), ValidationError> {
    visible(name, &format!("{kind} name"), MAX_TEXT)?;
    if !plausible_gib(size_gb) || size_gb == 0.0 {
        return invalid(format!("{kind} size is impossible"));
    }
    optional_visible(media_type.as_deref(), &format!("{kind} media type"))?;
    optional_visible(bus_type.as_deref(), &format!("{kind} bus type"))?;
    optional_visible(health_status.as_deref(), &format!("{kind} health status"))
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
            reference_station_readiness_state(&catalog, None),
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

    #[test]
    fn selected_blocked_tier_binds_blocked_readiness_instead_of_generic_unknown() {
        let input = StationBoxProbeInput {
            generated_at_unix_ms: 1_777_000_000_000,
            probe_collected_at_unix_ms: 1_777_000_000_000,
            max_probe_age_ms: 300_000,
            product_version: "0.1.0".into(),
            source_commit: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),
            station_id: Some("3f5f721f-96c7-48b1-b061-1bf1ad1e62c2".into()),
            station_name: Some("KTLT Community Television".into()),
            timezone: Some("America/Denver".into()),
            reference_station_tier_selection: Some(ReferenceStationTierSelection {
                catalog_version: REFERENCE_STATION_CATALOG_VERSION,
                tier: ReferenceStationTier::CableSdi,
                revision: 1,
                created_at_unix_ms: 1_777_000_000_000,
                updated_at_unix_ms: 1_777_000_000_000,
                updated_by: "operator-1".into(),
            }),
            hardware: HardwareProbeInput {
                logical_cpus: 16,
                ram_total_gb: 32.0,
                media_disk_free_gb: 500.0,
                machine_inventory: MachineInventoryEvidence {
                    collected_at_unix_ms: 1_777_000_000_000,
                    source: MachineEvidenceSource::WindowsCim,
                    gpu_collection: MachineInventoryCollectionState::Unavailable,
                    storage_collection: MachineInventoryCollectionState::Unavailable,
                    device_collection: MachineInventoryCollectionState::Unavailable,
                    media_volume_total_gb: None,
                    media_volume_backing_disks: Vec::new(),
                    media_volume_backing_disks_verified: false,
                    gpu_adapters: Vec::new(),
                    physical_disks: Vec::new(),
                    virtual_disks: Vec::new(),
                    ups_devices: Vec::new(),
                    decklink_devices: Vec::new(),
                    blackmagic_drivers: Vec::new(),
                },
            },
            engine: EngineProbeInput {
                gstreamer_version: Some("1.28.6".into()),
                required_factories: vec!["udpsink".into()],
                available_factories: vec!["udpsink".into()],
                available_encoders: vec!["openh264enc".into()],
                playout: ReadinessState::Pass,
                runtime: ReadinessState::Pass,
                output: ReadinessState::Pass,
                udp: CapabilitySupport::Supported,
                srt: CapabilitySupport::Supported,
                sdi: CapabilitySupport::Unimplemented,
                ndi: CapabilitySupport::Supported,
                interlaced: CapabilitySupport::Supported,
            },
            clock: ClockProbeInput {
                configured_timezone: Some("America/Denver".into()),
                utc_offset_seconds: Some(-21_600),
                sync_status: ClockSyncStatus::Synchronized,
            },
            network: NetworkProbeInput {
                state: ReadinessState::Pass,
                hostname: Some("station-one".into()),
                interface_up: true,
                dns_working: Some(true),
                outbound_udp: CapabilitySupport::Supported,
                outbound_srt: CapabilitySupport::Supported,
            },
            backup: BackupProbeInput {
                state: ReadinessState::Pass,
                destination_configured: true,
                writable: Some(true),
                detail: Some("backup probe passed".into()),
            },
            release: ReleaseProbeInput {
                state: ReadinessState::Pass,
                candidate_verified: true,
                update_available: false,
                detail: Some("candidate verified".into()),
            },
        };

        let profile = derive_station_box_profile(input.clone()).unwrap();
        assert_eq!(
            profile
                .reference_station_tier_selection
                .as_ref()
                .unwrap()
                .tier,
            ReferenceStationTier::CableSdi
        );
        assert_eq!(
            profile
                .peg_readiness
                .dimensions
                .iter()
                .find(|dimension| dimension.name == ReadinessDimensionName::ReferenceStation)
                .unwrap()
                .state,
            ReadinessState::Blocked
        );

        let mut future_selection = input.reference_station_tier_selection.clone().unwrap();
        future_selection.updated_at_unix_ms += 1;
        let mut future_input = input;
        future_input.reference_station_tier_selection = Some(future_selection);
        assert!(matches!(
            derive_station_box_profile(future_input),
            Err(ValidationError::InvalidInput(message))
                if message.contains("after profile generation")
        ));
    }

    #[test]
    fn selected_reference_state_mirrors_only_the_selected_current_assessment() {
        let catalog = ReferenceStationCatalog {
            version: REFERENCE_STATION_CATALOG_VERSION,
            matching_tiers: vec![ReferenceStationTier::Tier1Streaming],
            assessments: vec![
                ReferenceStationTierAssessment {
                    tier: ReferenceStationTier::Tier0BatchStream,
                    state: ReadinessState::Blocked,
                    requirements: Vec::new(),
                    detail: "blocked".into(),
                    next_step: None,
                },
                ReferenceStationTierAssessment {
                    tier: ReferenceStationTier::Tier1Streaming,
                    state: ReadinessState::Pass,
                    requirements: Vec::new(),
                    detail: "pass".into(),
                    next_step: None,
                },
                ReferenceStationTierAssessment {
                    tier: ReferenceStationTier::DevelopmentValidation,
                    state: ReadinessState::Unknown,
                    requirements: Vec::new(),
                    detail: "unknown".into(),
                    next_step: None,
                },
            ],
        };
        let selection = |tier| ReferenceStationTierSelection {
            catalog_version: REFERENCE_STATION_CATALOG_VERSION,
            tier,
            revision: 1,
            created_at_unix_ms: 100,
            updated_at_unix_ms: 100,
            updated_by: "operator-1".into(),
        };

        assert_eq!(
            reference_station_readiness_state(
                &catalog,
                Some(&selection(ReferenceStationTier::Tier1Streaming))
            ),
            ReadinessState::Pass
        );
        assert_eq!(
            reference_station_readiness_state(
                &catalog,
                Some(&selection(ReferenceStationTier::Tier0BatchStream))
            ),
            ReadinessState::Blocked
        );
        assert_eq!(
            reference_station_readiness_state(
                &catalog,
                Some(&selection(ReferenceStationTier::DevelopmentValidation))
            ),
            ReadinessState::Unknown
        );
        assert_eq!(
            reference_station_readiness_state(&catalog, None),
            ReadinessState::Unknown
        );

        let mut old = selection(ReferenceStationTier::Tier1Streaming);
        old.catalog_version -= 1;
        assert_eq!(
            reference_station_readiness_state(&catalog, Some(&old)),
            ReadinessState::Unknown
        );
    }
}
