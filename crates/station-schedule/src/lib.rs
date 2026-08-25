use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum OutputProfileId {
    #[default]
    #[serde(rename = "proof-360p30")]
    Proof360p30,
    #[serde(rename = "atsc-720p5994")]
    Atsc720p5994,
    #[serde(rename = "atsc-1080i5994")]
    Atsc1080i5994,
    #[serde(rename = "ntsc-480i2997")]
    Ntsc480i2997,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct OutputProfile {
    pub profile_id: OutputProfileId,
    pub display_name: &'static str,
    pub intended_use: &'static str,
    pub width: u32,
    pub height: u32,
    pub frame_rate_numerator: u32,
    pub frame_rate_denominator: u32,
    pub interlaced: bool,
    pub video_bitrate_bps: u32,
    pub audio_bitrate_bps: u32,
    pub transport_bitrate_bps: u64,
    pub audio_sample_rate_hz: u32,
    pub loudness_target_lufs: f32,
}

pub const OUTPUT_PROFILE_CONTRACTS: [OutputProfile; 4] = [
    OutputProfile {
        profile_id: OutputProfileId::Proof360p30,
        display_name: "Local proof — 360p30",
        intended_use: "Loopback commissioning and low-load machine proof only",
        width: 640,
        height: 360,
        frame_rate_numerator: 30,
        frame_rate_denominator: 1,
        interlaced: false,
        video_bitrate_bps: 1_000_000,
        audio_bitrate_bps: 128_000,
        transport_bitrate_bps: 1_500_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -16.0,
    },
    OutputProfile {
        profile_id: OutputProfileId::Atsc720p5994,
        display_name: "ATSC/IP cable — 720p59.94",
        intended_use: "Progressive HD MPEG-TS handoff to an IP-capable headend",
        width: 1_280,
        height: 720,
        frame_rate_numerator: 60_000,
        frame_rate_denominator: 1_001,
        interlaced: false,
        video_bitrate_bps: 6_000_000,
        audio_bitrate_bps: 192_000,
        transport_bitrate_bps: 8_000_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -24.0,
    },
    OutputProfile {
        profile_id: OutputProfileId::Atsc1080i5994,
        display_name: "ATSC cable — 1080i59.94",
        intended_use: "Interlaced HD cable-headend delivery contract; execution is not yet implemented",
        width: 1_920,
        height: 1_080,
        frame_rate_numerator: 30_000,
        frame_rate_denominator: 1_001,
        interlaced: true,
        video_bitrate_bps: 10_000_000,
        audio_bitrate_bps: 384_000,
        transport_bitrate_bps: 12_000_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -24.0,
    },
    OutputProfile {
        profile_id: OutputProfileId::Ntsc480i2997,
        display_name: "NTSC cable — 480i29.97",
        intended_use: "Interlaced SD cable-headend delivery contract; execution is not yet implemented",
        width: 720,
        height: 480,
        frame_rate_numerator: 30_000,
        frame_rate_denominator: 1_001,
        interlaced: true,
        video_bitrate_bps: 3_180_000,
        audio_bitrate_bps: 192_000,
        transport_bitrate_bps: 3_750_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -24.0,
    },
];

/// Profiles that the current engine can actually execute and the operator may select.
/// Interlaced contracts remain available to headend staging/readiness without becoming
/// selectable output until their engine path is implemented and proved.
pub const OUTPUT_PROFILES: [OutputProfile; 2] =
    [OUTPUT_PROFILE_CONTRACTS[0], OUTPUT_PROFILE_CONTRACTS[1]];

impl OutputProfileId {
    pub fn profile(self) -> &'static OutputProfile {
        OUTPUT_PROFILE_CONTRACTS
            .iter()
            .find(|profile| profile.profile_id == self)
            .expect("every output profile identifier has one specification")
    }

    pub const fn storage_name(self) -> &'static str {
        match self {
            Self::Proof360p30 => "proof-360p30",
            Self::Atsc720p5994 => "atsc-720p5994",
            Self::Atsc1080i5994 => "atsc-1080i5994",
            Self::Ntsc480i2997 => "ntsc-480i2997",
        }
    }

    pub fn from_storage_name(value: &str) -> Result<Self, ScheduleError> {
        match value {
            "proof-360p30" => Ok(Self::Proof360p30),
            "atsc-720p5994" => Ok(Self::Atsc720p5994),
            "atsc-1080i5994" => Ok(Self::Atsc1080i5994),
            "ntsc-480i2997" => Ok(Self::Ntsc480i2997),
            _ => Err(ScheduleError::Invalid("output_profile is unsupported")),
        }
    }

    pub fn is_runnable(self) -> bool {
        OUTPUT_PROFILES
            .iter()
            .any(|profile| profile.profile_id == self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum OutputSinkKind {
    UdpTs,
    Srt,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutputSinkConfiguration {
    pub sink_id: String,
    pub label: String,
    pub kind: OutputSinkKind,
    pub destination: String,
    pub loudness_target_lufs: f32,
    pub required: bool,
    pub enabled: bool,
    #[serde(default)]
    pub encryption_configured: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub headend: Option<HeadendSinkContract>,
}

impl OutputSinkConfiguration {
    pub fn required_udp(
        sink_id: impl Into<String>,
        label: impl Into<String>,
        destination: impl Into<String>,
        loudness_target_lufs: f32,
    ) -> Self {
        Self {
            sink_id: sink_id.into(),
            label: label.into(),
            kind: OutputSinkKind::UdpTs,
            destination: destination.into(),
            loudness_target_lufs,
            required: true,
            enabled: true,
            encryption_configured: false,
            headend: None,
        }
    }

    pub fn required_srt(
        sink_id: impl Into<String>,
        label: impl Into<String>,
        destination: impl Into<String>,
        loudness_target_lufs: f32,
    ) -> Self {
        Self {
            sink_id: sink_id.into(),
            label: label.into(),
            kind: OutputSinkKind::Srt,
            destination: destination.into(),
            loudness_target_lufs,
            required: true,
            enabled: true,
            encryption_configured: false,
            headend: None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum HeadendProfileId {
    GenericUdpSpts,
    ComcastMtdSd,
    ComcastMtdHd,
    TelvueHypercasterIp,
    HarmonicSpectrumTs,
    LeightronixFileDrop,
}

impl HeadendProfileId {
    pub const fn storage_name(self) -> &'static str {
        match self {
            Self::GenericUdpSpts => "generic-udp-spts",
            Self::ComcastMtdSd => "comcast-mtd-sd",
            Self::ComcastMtdHd => "comcast-mtd-hd",
            Self::TelvueHypercasterIp => "telvue-hypercaster-ip",
            Self::HarmonicSpectrumTs => "harmonic-spectrum-ts",
            Self::LeightronixFileDrop => "leightronix-file-drop",
        }
    }

    pub fn from_storage_name(value: &str) -> Result<Self, ScheduleError> {
        match value {
            "generic-udp-spts" => Ok(Self::GenericUdpSpts),
            "comcast-mtd-sd" => Ok(Self::ComcastMtdSd),
            "comcast-mtd-hd" => Ok(Self::ComcastMtdHd),
            "telvue-hypercaster-ip" => Ok(Self::TelvueHypercasterIp),
            "harmonic-spectrum-ts" => Ok(Self::HarmonicSpectrumTs),
            "leightronix-file-drop" => Ok(Self::LeightronixFileDrop),
            _ => Err(ScheduleError::Invalid("headend profile is unsupported")),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum HeadendTransport {
    UdpUnicast,
    UdpMulticast,
    FileDrop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HeadendProfileExecutionState {
    Runnable,
    StagedOnly,
    Unimplemented,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendSinkContract {
    pub profile_id: HeadendProfileId,
    pub muxrate_kbps: u32,
    pub packet_size_bytes: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct HeadendProfile {
    pub profile_id: HeadendProfileId,
    pub label: &'static str,
    pub vendor: &'static str,
    pub source_urls: &'static [&'static str],
    pub canonical_profile: OutputProfileId,
    pub muxrate_kbps: u32,
    pub transport: HeadendTransport,
    pub packet_size_bytes: u16,
    pub minimum_port: u16,
    pub operator_must_supply: &'static [&'static str],
    pub not_claimed: &'static [&'static str],
    pub execution_state: HeadendProfileExecutionState,
    pub execution_detail: &'static str,
}

const FIELD_PROOF_BOUNDARY: &str =
    "Built from published vendor contracts; field and headend acceptance are not claimed.";
const DESTINATION_AND_RATE: &[&str] = &[
    "Destination address and UDP port assigned by the cable operator or headend integrator.",
    "Constant multiplex rate when the carriage agreement sets one.",
];

pub const HEADEND_PROFILES: [HeadendProfile; 6] = [
    HeadendProfile {
        profile_id: HeadendProfileId::GenericUdpSpts,
        label: "Generic CBR SPTS over UDP",
        vendor: "Any constant-rate MPEG transport-stream headend",
        source_urls: &[
            "https://telvue.com/knowledgebase/feed-setup-encoder-configuration/",
            "https://www.pixeltools.com/tech_tip_cablelabs.html",
        ],
        canonical_profile: OutputProfileId::Atsc720p5994,
        muxrate_kbps: 8_000,
        transport: HeadendTransport::UdpUnicast,
        packet_size_bytes: 1_316,
        minimum_port: 1,
        operator_must_supply: DESTINATION_AND_RATE,
        not_claimed: &[FIELD_PROOF_BOUNDARY],
        execution_state: HeadendProfileExecutionState::Runnable,
        execution_detail: "The progressive H.264/AAC 8 Mb/s UDP SPTS path is locally executable; downstream headend acceptance still requires receiver evidence.",
    },
    HeadendProfile {
        profile_id: HeadendProfileId::ComcastMtdSd,
        label: "Comcast MTD — SD",
        vendor: "Comcast Technology Solutions, Managed Terrestrial Distribution",
        source_urls: &[
            "https://www.comcasttechnologysolutions.com/managed-terrestrial-distribution",
            "https://www.pixeltools.com/tech_tip_cablelabs.html",
        ],
        canonical_profile: OutputProfileId::Ntsc480i2997,
        muxrate_kbps: 3_750,
        transport: HeadendTransport::UdpMulticast,
        packet_size_bytes: 1_316,
        minimum_port: 1,
        operator_must_supply: &["Multicast group address and UDP port assigned by Comcast."],
        not_claimed: &[FIELD_PROOF_BOUNDARY],
        execution_state: HeadendProfileExecutionState::StagedOnly,
        execution_detail: "Configuration may be staged while stopped; interlaced MPEG-2/AC-3 execution must be implemented and proved before enablement.",
    },
    HeadendProfile {
        profile_id: HeadendProfileId::ComcastMtdHd,
        label: "Comcast MTD — HD",
        vendor: "Comcast Technology Solutions, Managed Terrestrial Distribution",
        source_urls: &[
            "https://www.comcasttechnologysolutions.com/managed-terrestrial-distribution",
        ],
        canonical_profile: OutputProfileId::Atsc1080i5994,
        muxrate_kbps: 12_000,
        transport: HeadendTransport::UdpMulticast,
        packet_size_bytes: 1_316,
        minimum_port: 1,
        operator_must_supply: &[
            "Multicast group address and UDP port assigned by Comcast.",
            "Constant multiplex rate from the carriage agreement; 12 Mbps is only the catalog default.",
        ],
        not_claimed: &[FIELD_PROOF_BOUNDARY],
        execution_state: HeadendProfileExecutionState::StagedOnly,
        execution_detail: "Configuration may be staged while stopped; interlaced AC-3 execution must be implemented and proved before enablement.",
    },
    HeadendProfile {
        profile_id: HeadendProfileId::TelvueHypercasterIp,
        label: "TelVue HyperCaster — IP transport stream input",
        vendor: "TelVue HyperCaster",
        source_urls: &[
            "https://telvue.com/knowledgebase/feed-setup-encoder-configuration/",
            "https://telvue.com/knowledgebase/preparing-content-for-the-hypercaster/",
            "https://telvue.com/knowledgebase/configure-inout-ports/",
        ],
        canonical_profile: OutputProfileId::Atsc720p5994,
        muxrate_kbps: 8_000,
        transport: HeadendTransport::UdpUnicast,
        packet_size_bytes: 1_316,
        minimum_port: 1_024,
        operator_must_supply: &[
            "HyperCaster feed address and port from 1024 through 65535.",
            "A feed Max Bit Rate matching the selected multiplex rate.",
        ],
        not_claimed: &[FIELD_PROOF_BOUNDARY],
        execution_state: HeadendProfileExecutionState::Runnable,
        execution_detail: "The progressive H.264/AAC 8 Mb/s UDP input path is locally executable; HyperCaster acceptance still requires receiver evidence.",
    },
    HeadendProfile {
        profile_id: HeadendProfileId::HarmonicSpectrumTs,
        label: "Harmonic Spectrum — transport stream ingest",
        vendor: "Harmonic Spectrum X / XE",
        source_urls: &[
            "https://www.harmonicinc.com/hubfs/datasheet/spectrum-x.pdf",
            "https://www.harmonicinc.com/hubfs/datasheet/spectrum-xe.pdf",
        ],
        canonical_profile: OutputProfileId::Atsc1080i5994,
        muxrate_kbps: 12_000,
        transport: HeadendTransport::UdpUnicast,
        packet_size_bytes: 1_316,
        minimum_port: 1,
        operator_must_supply: DESTINATION_AND_RATE,
        not_claimed: &[FIELD_PROOF_BOUNDARY],
        execution_state: HeadendProfileExecutionState::StagedOnly,
        execution_detail: "Configuration may be staged while stopped; interlaced AC-3 execution must be implemented and proved before enablement.",
    },
    HeadendProfile {
        profile_id: HeadendProfileId::LeightronixFileDrop,
        label: "Leightronix UltraNEXUS — file handoff",
        vendor: "Leightronix UltraNEXUS-HD",
        source_urls: &[
            "https://www.leightronix.com/ultranexus-hd-series.html",
            "https://support.leightronix.com/ultranexus-hd",
        ],
        canonical_profile: OutputProfileId::Atsc720p5994,
        muxrate_kbps: 0,
        transport: HeadendTransport::FileDrop,
        packet_size_bytes: 1_316,
        minimum_port: 1,
        operator_must_supply: &["Drop-folder path monitored by the UltraNEXUS."],
        not_claimed: &[FIELD_PROOF_BOUNDARY],
        execution_state: HeadendProfileExecutionState::Unimplemented,
        execution_detail: "The file-drop delivery worker is not implemented and this profile cannot yet be applied.",
    },
];

impl HeadendProfile {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if self.label.trim().is_empty()
            || self.vendor.trim().is_empty()
            || self.source_urls.is_empty()
            || self.operator_must_supply.is_empty()
            || self.not_claimed.is_empty()
            || self.execution_detail.trim().is_empty()
            || self.packet_size_bytes != 1_316
            || (self.transport != HeadendTransport::FileDrop && self.muxrate_kbps == 0)
            || (self.transport == HeadendTransport::FileDrop && self.muxrate_kbps != 0)
        {
            return Err(ScheduleError::Invalid(
                "headend profile metadata is invalid",
            ));
        }
        Ok(())
    }
}

pub fn headend_profile(profile_id: &str) -> Option<&'static HeadendProfile> {
    HEADEND_PROFILES
        .iter()
        .find(|profile| profile.profile_id.storage_name() == profile_id)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendProfileApplyRequest {
    pub profile_id: String,
    pub destination_uri: String,
    pub muxrate_kbps: Option<u32>,
    #[serde(default)]
    pub keep_existing_sinks: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HeadendProfileApplication {
    pub channel: ChannelConfiguration,
    pub profile_id: HeadendProfileId,
    pub muxrate_kbps: u32,
    pub packet_size_bytes: u16,
    pub not_claimed: Vec<String>,
}

pub fn apply_headend_profile(
    channel: &ChannelConfiguration,
    request: &HeadendProfileApplyRequest,
) -> Result<HeadendProfileApplication, ScheduleError> {
    channel.validate()?;
    if channel.enabled {
        return Err(ScheduleError::Invalid(
            "a channel must be stopped before applying a headend profile",
        ));
    }
    let profile = headend_profile(&request.profile_id)
        .ok_or(ScheduleError::Invalid("headend profile is unsupported"))?;
    profile.validate()?;
    if profile.transport == HeadendTransport::FileDrop {
        return Err(ScheduleError::Invalid(
            "file-drop headend execution is not implemented",
        ));
    }
    let destination = parse_headend_udp_destination(profile, &request.destination_uri)?;
    let muxrate_kbps = request.muxrate_kbps.unwrap_or(profile.muxrate_kbps);
    let encoded_kbps = (u64::from(profile.canonical_profile.profile().video_bitrate_bps)
        + u64::from(profile.canonical_profile.profile().audio_bitrate_bps))
        / 1_000;
    if u64::from(muxrate_kbps) <= encoded_kbps || muxrate_kbps > 100_000 {
        return Err(ScheduleError::Invalid(
            "headend muxrate must exceed encoded payload and be at most 100000 kbps",
        ));
    }
    let sink_id = deterministic_headend_sink_id(&channel.channel_id);
    let sink = OutputSinkConfiguration {
        sink_id,
        label: "Cable headend".into(),
        kind: OutputSinkKind::UdpTs,
        destination,
        loudness_target_lufs: -24.0,
        required: true,
        enabled: true,
        encryption_configured: false,
        headend: Some(HeadendSinkContract {
            profile_id: profile.profile_id,
            muxrate_kbps,
            packet_size_bytes: profile.packet_size_bytes,
        }),
    };
    let mut sinks = if request.keep_existing_sinks {
        channel
            .output_sinks
            .iter()
            .filter(|existing| existing.headend.is_none())
            .cloned()
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    sinks.push(sink);
    let applied = ChannelConfiguration {
        channel_id: channel.channel_id.clone(),
        display_name: channel.display_name.clone(),
        output_profile: profile.canonical_profile,
        output_sinks: sinks,
        enabled: false,
    };
    applied.validate()?;
    Ok(HeadendProfileApplication {
        channel: applied,
        profile_id: profile.profile_id,
        muxrate_kbps,
        packet_size_bytes: profile.packet_size_bytes,
        not_claimed: profile
            .not_claimed
            .iter()
            .map(|value| (*value).into())
            .collect(),
    })
}

fn parse_headend_udp_destination(
    profile: &HeadendProfile,
    destination_uri: &str,
) -> Result<String, ScheduleError> {
    let authority = destination_uri
        .strip_prefix("udp://")
        .ok_or(ScheduleError::Invalid(
            "headend destination must use udp:// followed by an IP address and port",
        ))?;
    if authority.contains(['/', '?', '#', '@']) {
        return Err(ScheduleError::Invalid(
            "headend destination cannot contain paths, options, or credentials",
        ));
    }
    let destination = authority.parse::<std::net::SocketAddr>().map_err(|_| {
        ScheduleError::Invalid("headend destination must contain an IP address and explicit port")
    })?;
    if destination.port() < profile.minimum_port {
        return Err(ScheduleError::Invalid(
            "headend destination port is below the vendor contract minimum",
        ));
    }
    let multicast = destination.ip().is_multicast();
    if (profile.transport == HeadendTransport::UdpMulticast && !multicast)
        || (profile.transport == HeadendTransport::UdpUnicast && multicast)
    {
        return Err(ScheduleError::Invalid(
            "headend destination address does not match the profile transport",
        ));
    }
    Ok(destination.to_string())
}

fn deterministic_headend_sink_id(channel_id: &str) -> String {
    let mut bytes = [0_u8; 16];
    for (index, byte) in channel_id.bytes().filter(|byte| *byte != b'-').enumerate() {
        let value = match byte {
            b'0'..=b'9' => byte - b'0',
            b'a'..=b'f' => byte - b'a' + 10,
            _ => 0,
        };
        if index % 2 == 0 {
            bytes[index / 2] = value << 4;
        } else {
            bytes[index / 2] |= value;
        }
    }
    for (index, byte) in b"townlight-headend".iter().enumerate() {
        bytes[index % 16] ^= *byte;
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    )
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MediaAsset {
    pub asset_id: String,
    pub media_path: String,
    pub duration_ms: u64,
    pub readiness: AssetReadiness,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetReadiness {
    Ready,
    Missing,
    Rejected,
    Processing,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleItem {
    pub item_id: String,
    pub revision: u64,
    pub channel_id: String,
    pub asset_id: String,
    pub title: String,
    pub starts_at_unix_ms: i64,
    pub duration_ms: u64,
    pub state: ScheduleState,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChannelConfiguration {
    pub channel_id: String,
    pub display_name: String,
    #[serde(default)]
    pub output_profile: OutputProfileId,
    pub output_sinks: Vec<OutputSinkConfiguration>,
    pub enabled: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScheduleState {
    Draft,
    Committed,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScheduleWindow {
    Future,
    Active { media_offset_ms: u64 },
    Ended,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleConflict {
    pub existing_item_id: String,
    pub overlap_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleGap {
    pub prior_item_id: String,
    pub gap_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitPlan {
    pub plan_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub dry_run_passed: bool,
    pub conflicts: Vec<ScheduleConflict>,
    pub missing_media_detail: Option<String>,
    pub gaps: Vec<ScheduleGap>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitReport {
    pub report_id: String,
    pub plan_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub asset_id: String,
    pub approved_by: String,
    pub approved_at_unix_ms: i64,
    pub dispatch_status: DispatchStatus,
    pub operator_notes: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DispatchStatus {
    Pending,
    Queued,
    Acknowledged,
    Missed,
    Completed,
    Error,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DispatchJob {
    pub report: CommitReport,
    pub item: ScheduleItem,
    pub asset: MediaAsset,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleError {
    Invalid(&'static str),
    TimeOverflow,
}

impl MediaAsset {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if !is_asset_id(&self.asset_id) {
            return Err(ScheduleError::Invalid(
                "asset_id must be a lowercase SHA-256 digest",
            ));
        }
        if self.media_path.trim().is_empty() || self.media_path.chars().any(char::is_control) {
            return Err(ScheduleError::Invalid("media_path must be a visible path"));
        }
        if self.duration_ms == 0 {
            return Err(ScheduleError::Invalid("asset duration must be positive"));
        }
        Ok(())
    }
}

impl ScheduleItem {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if !is_uuid(&self.item_id) || !is_uuid(&self.channel_id) {
            return Err(ScheduleError::Invalid(
                "item_id and channel_id must be canonical UUIDs",
            ));
        }
        if self.revision == 0 {
            return Err(ScheduleError::Invalid(
                "schedule item revision must be nonzero",
            ));
        }
        if !is_asset_id(&self.asset_id) {
            return Err(ScheduleError::Invalid(
                "asset_id must be a lowercase SHA-256 digest",
            ));
        }
        if self.title.trim().is_empty()
            || self.title.len() > 240
            || self.title.chars().any(char::is_control)
        {
            return Err(ScheduleError::Invalid(
                "title must contain 1 to 240 visible characters",
            ));
        }
        if self.starts_at_unix_ms < 0 || self.duration_ms == 0 {
            return Err(ScheduleError::Invalid(
                "start time must be nonnegative and duration must be positive",
            ));
        }
        self.ends_at_unix_ms()?;
        Ok(())
    }

    pub fn ends_at_unix_ms(&self) -> Result<i64, ScheduleError> {
        let duration = i64::try_from(self.duration_ms).map_err(|_| ScheduleError::TimeOverflow)?;
        self.starts_at_unix_ms
            .checked_add(duration)
            .ok_or(ScheduleError::TimeOverflow)
    }

    /// Classifies the item's half-open wall-clock window. At an exact end
    /// boundary the item is no longer eligible; an item beginning at that
    /// same instant is eligible at media offset zero.
    pub fn window_at(&self, now_unix_ms: i64) -> Result<ScheduleWindow, ScheduleError> {
        if now_unix_ms < self.starts_at_unix_ms {
            return Ok(ScheduleWindow::Future);
        }
        if now_unix_ms >= self.ends_at_unix_ms()? {
            return Ok(ScheduleWindow::Ended);
        }
        let elapsed = now_unix_ms
            .checked_sub(self.starts_at_unix_ms)
            .ok_or(ScheduleError::TimeOverflow)?;
        let media_offset_ms = u64::try_from(elapsed).map_err(|_| ScheduleError::TimeOverflow)?;
        Ok(ScheduleWindow::Active { media_offset_ms })
    }
}

impl ChannelConfiguration {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if !is_uuid(&self.channel_id) {
            return Err(ScheduleError::Invalid(
                "channel_id must be a canonical UUID",
            ));
        }
        if self.display_name.trim().is_empty()
            || self.display_name.len() > 120
            || self.display_name.chars().any(char::is_control)
        {
            return Err(ScheduleError::Invalid(
                "display_name must contain 1 to 120 visible characters",
            ));
        }
        let profile = self.output_profile.profile();
        if profile.width == 0
            || profile.height == 0
            || profile.frame_rate_numerator == 0
            || profile.frame_rate_denominator == 0
            || profile.video_bitrate_bps == 0
            || profile.audio_bitrate_bps == 0
            || profile.transport_bitrate_bps
                <= u64::from(profile.video_bitrate_bps) + u64::from(profile.audio_bitrate_bps)
            || profile.audio_sample_rate_hz != 48_000
        {
            return Err(ScheduleError::Invalid(
                "output_profile does not define a runnable broadcast contract",
            ));
        }
        if self.enabled && profile.interlaced {
            return Err(ScheduleError::Invalid(
                "interlaced output cannot be enabled until the media engine implements it",
            ));
        }
        if self.output_sinks.is_empty() {
            return Err(ScheduleError::Invalid(
                "a channel must define at least one output sink",
            ));
        }
        let mut sink_ids = HashSet::new();
        let mut labels = HashSet::new();
        for sink in &self.output_sinks {
            if !is_uuid(&sink.sink_id) || !sink_ids.insert(sink.sink_id.as_str()) {
                return Err(ScheduleError::Invalid(
                    "output sink identities must be unique canonical UUIDs",
                ));
            }
            if sink.label.trim() != sink.label
                || sink.label.is_empty()
                || sink.label.len() > 120
                || sink.label.chars().any(char::is_control)
                || !labels.insert(sink.label.to_lowercase())
            {
                return Err(ScheduleError::Invalid(
                    "output sink labels must be unique, trimmed, and 1-120 visible characters",
                ));
            }
            match sink.kind {
                OutputSinkKind::UdpTs => {
                    if sink.encryption_configured {
                        return Err(ScheduleError::Invalid(
                            "UDP outputs cannot carry SRT encryption configuration",
                        ));
                    }
                    let destination =
                        sink.destination
                            .parse::<std::net::SocketAddr>()
                            .map_err(|_| {
                                ScheduleError::Invalid(
                                    "UDP transport destinations must be an IP address and port",
                                )
                            })?;
                    if destination.port() == 0 {
                        return Err(ScheduleError::Invalid(
                            "UDP transport destination ports must be nonzero",
                        ));
                    }
                }
                OutputSinkKind::Srt => {
                    if sink.enabled && !sink.required {
                        return Err(ScheduleError::Invalid(
                            "enabled SRT outputs must be required until independent branch health is available",
                        ));
                    }
                    let Some(authority) = sink.destination.strip_prefix("srt://") else {
                        return Err(ScheduleError::Invalid(
                            "SRT destinations must use srt:// followed by an IP address and port",
                        ));
                    };
                    if authority.contains(['?', '#', '@']) {
                        return Err(ScheduleError::Invalid(
                            "SRT destination options and credentials are not allowed in the public URI",
                        ));
                    }
                    let destination = authority.parse::<std::net::SocketAddr>().map_err(|_| {
                        ScheduleError::Invalid(
                            "SRT destinations must use srt:// followed by an IP address and port",
                        )
                    })?;
                    if destination.port() == 0 {
                        return Err(ScheduleError::Invalid(
                            "SRT destination ports must be nonzero",
                        ));
                    }
                }
            }
            if !sink.loudness_target_lufs.is_finite()
                || !(-70.0..=-5.0).contains(&sink.loudness_target_lufs)
            {
                return Err(ScheduleError::Invalid(
                    "output sink loudness targets must be finite and between -70 and -5 LUFS",
                ));
            }
            let milli_lufs = sink.loudness_target_lufs * 1_000.0;
            if (milli_lufs - milli_lufs.round()).abs() > 0.01 {
                return Err(ScheduleError::Invalid(
                    "output sink loudness targets must have at most three decimal places",
                ));
            }
            if (sink.loudness_target_lufs - (-24.0)).abs() > f32::EPSILON
                && (sink.loudness_target_lufs - (-16.0)).abs() > f32::EPSILON
            {
                return Err(ScheduleError::Invalid(
                    "output sink loudness target must be an executable -24 or -16 LUFS variant",
                ));
            }
            if let Some(headend) = &sink.headend {
                let named = HEADEND_PROFILES
                    .iter()
                    .find(|profile| profile.profile_id == headend.profile_id)
                    .ok_or(ScheduleError::Invalid(
                        "headend sink profile is unsupported",
                    ))?;
                let destination =
                    sink.destination
                        .parse::<std::net::SocketAddr>()
                        .map_err(|_| {
                            ScheduleError::Invalid(
                                "headend sink destination is not an IP address and port",
                            )
                        })?;
                let multicast = destination.ip().is_multicast();
                let encoded_kbps = (u64::from(profile.video_bitrate_bps)
                    + u64::from(profile.audio_bitrate_bps))
                    / 1_000;
                if sink.kind != OutputSinkKind::UdpTs
                    || named.transport == HeadendTransport::FileDrop
                    || named.canonical_profile != self.output_profile
                    || headend.packet_size_bytes != named.packet_size_bytes
                    || u64::from(headend.muxrate_kbps) <= encoded_kbps
                    || headend.muxrate_kbps > 100_000
                    || destination.port() < named.minimum_port
                    || (named.transport == HeadendTransport::UdpMulticast && !multicast)
                    || (named.transport == HeadendTransport::UdpUnicast && multicast)
                    || (sink.loudness_target_lufs - (-24.0)).abs() > f32::EPSILON
                {
                    return Err(ScheduleError::Invalid(
                        "headend sink contract does not match the selected profile",
                    ));
                }
                if self.enabled {
                    if named.execution_state != HeadendProfileExecutionState::Runnable {
                        return Err(ScheduleError::Invalid(
                            "headend output cannot be enabled until its profile is executable",
                        ));
                    }
                    let emitted_muxrate_kbps = profile.transport_bitrate_bps / 1_000;
                    if !profile.transport_bitrate_bps.is_multiple_of(1_000)
                        || u64::from(headend.muxrate_kbps) != emitted_muxrate_kbps
                    {
                        return Err(ScheduleError::Invalid(
                            "headend muxrate cannot be enabled until the media engine emits that exact rate",
                        ));
                    }
                }
            }
        }
        if self.enabled && !self.output_sinks.iter().any(|sink| sink.enabled) {
            return Err(ScheduleError::Invalid(
                "an enabled channel must have at least one enabled output sink",
            ));
        }
        Ok(())
    }
}

pub fn prepare_commit(
    plan_id: impl Into<String>,
    proposed: &ScheduleItem,
    asset: Option<&MediaAsset>,
    channel_items: &[ScheduleItem],
) -> Result<CommitPlan, ScheduleError> {
    proposed.validate()?;
    let proposed_end = proposed.ends_at_unix_ms()?;
    let missing_media_detail = match asset {
        None => Some(format!("Asset {} does not exist.", proposed.asset_id)),
        Some(asset) if asset.validate().is_err() => Some("The asset record is invalid.".into()),
        Some(asset) if asset.asset_id != proposed.asset_id => {
            Some("The schedule item and asset identities do not match.".into())
        }
        Some(asset) if asset.readiness != AssetReadiness::Ready => Some(format!(
            "Asset {} is not ready for air ({:?}).",
            asset.asset_id, asset.readiness
        )),
        Some(asset) if asset.duration_ms < proposed.duration_ms => Some(format!(
            "Asset {} is shorter than the scheduled duration.",
            asset.asset_id
        )),
        Some(_) => None,
    };

    let mut conflicts = Vec::new();
    let mut prior: Option<(&ScheduleItem, i64)> = None;
    for existing in channel_items {
        if existing.channel_id != proposed.channel_id
            || existing.item_id == proposed.item_id
            || existing.state == ScheduleState::Cancelled
        {
            continue;
        }
        existing.validate()?;
        let existing_end = existing.ends_at_unix_ms()?;
        if proposed.starts_at_unix_ms < existing_end && existing.starts_at_unix_ms < proposed_end {
            let overlap_start = proposed.starts_at_unix_ms.max(existing.starts_at_unix_ms);
            let overlap_end = proposed_end.min(existing_end);
            conflicts.push(ScheduleConflict {
                existing_item_id: existing.item_id.clone(),
                overlap_ms: u64::try_from(overlap_end - overlap_start)
                    .map_err(|_| ScheduleError::TimeOverflow)?,
            });
        }
        if existing_end <= proposed.starts_at_unix_ms
            && prior.is_none_or(|(_, prior_end)| existing_end > prior_end)
        {
            prior = Some((existing, existing_end));
        }
    }
    conflicts.sort_by(|left, right| left.existing_item_id.cmp(&right.existing_item_id));
    let gaps = prior
        .and_then(|(item, end)| {
            let gap = proposed.starts_at_unix_ms - end;
            (gap > 1_000).then(|| ScheduleGap {
                prior_item_id: item.item_id.clone(),
                gap_ms: gap as u64,
            })
        })
        .into_iter()
        .collect();

    Ok(CommitPlan {
        plan_id: plan_id.into(),
        channel_id: proposed.channel_id.clone(),
        schedule_item_id: proposed.item_id.clone(),
        dry_run_passed: missing_media_detail.is_none() && conflicts.is_empty(),
        conflicts,
        missing_media_detail,
        gaps,
    })
}

fn is_asset_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}
