//! Bounded Windows machine-inventory collection for StationBox evidence.
//!
//! The PowerShell program is deliberately a fixed string.  The only machine
//! value supplied to it is the media path through an environment variable;
//! paths and operator input never become script text or command arguments.

use std::path::Path;
use std::time::Duration;

use serde::Deserialize;
use station_readiness::{
    BlackmagicDriverEvidence, DeckLinkDeviceEvidence, GpuAdapterEvidence, MachineEvidenceSource,
    MachineInventoryCollectionState, MachineInventoryEvidence, MediaVolumeBackingDiskEvidence,
    PhysicalDiskEvidence, UpsDeviceEvidence, VirtualDiskEvidence,
};

use super::{hidden_command, run_bounded_command};

const MEDIA_PATH_ENV: &str = "TOWNLIGHT_STATION_MEDIA_PROBE_PATH";
const COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_OUTPUT_BYTES: u64 = 1024 * 1024;

/// This is the sole script passed to Windows PowerShell. Keep it static.
pub(crate) const WINDOWS_MACHINE_INVENTORY_SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$mediaPath = [Environment]::GetEnvironmentVariable('TOWNLIGHT_STATION_MEDIA_PROBE_PATH')

function Text([object] $value) {
    if ($null -eq $value) { return $null }
    $text = ([string]$value).Trim()
    if ($text.Length -eq 0) { return $null }
    return $text
}

$gpu = @{ state = 'unavailable'; facts = @() }
try {
    $adapters = @(Get-CimInstance Win32_VideoController | ForEach-Object {
        @{ name = Text $_.Name; driver_version = Text $_.DriverVersion;
           vram_bytes = [int64]$_.AdapterRAM }
    })
    $gpu = @{ state = 'collected'; facts = $adapters }
} catch {
}

$storage = @{ state = 'unavailable'; media_total_bytes = $null;
              media_volume_backing_disks = @(); media_volume_backing_disks_verified = $false;
              physical_disks = @(); virtual_disks = @() }
try {
    if ([string]::IsNullOrWhiteSpace($mediaPath)) { throw 'media path is missing' }
    $volumeCandidates = @(Get-Volume -FilePath $mediaPath -ErrorAction Stop)
    if ($volumeCandidates.Count -ne 1) { throw 'media volume is ambiguous or unavailable' }
    $volume = $volumeCandidates[0]
    $volumeIdentity = Text $volume.UniqueId
    if ($null -eq $volumeIdentity) { throw 'media volume identity is unavailable' }
    $volumeSizeBytes = [int64]$volume.Size
    if ($volumeSizeBytes -le 0) { throw 'media volume size is unavailable' }
    $partitions = @(Get-Partition -Volume $volume -ErrorAction Stop)
    if ($partitions.Count -eq 0) { throw 'media volume partition is unavailable' }
    $diskNumbers = @($partitions | Select-Object -ExpandProperty DiskNumber -Unique | Sort-Object { [int]$_ })
    if ($diskNumbers.Count -eq 0) { throw 'media volume backing disk is unavailable' }
    if ($diskNumbers.Count -gt 256) { throw 'media volume backing disk set is too large' }
    $backingDiskObjects = @($diskNumbers | ForEach-Object { Get-Disk -Number $_ -ErrorAction Stop })
    if ($backingDiskObjects.Count -ne $diskNumbers.Count) { throw 'media volume backing disk set is incomplete' }
    [decimal]$backingSizeBytes = 0
    foreach ($disk in $backingDiskObjects) {
        $diskSizeBytes = [int64]$disk.Size
        if ($diskSizeBytes -le 0) { throw 'media volume backing disk size is unavailable' }
        $backingSizeBytes += [decimal]$diskSizeBytes
    }
    if ($backingSizeBytes -lt [decimal]$volumeSizeBytes) { throw 'media volume backing disks do not cover volume size' }
    $backing = @($backingDiskObjects | ForEach-Object {
        @{ name = Text $_.FriendlyName; media_type = Text $_.MediaType;
           bus_type = Text $_.BusType; size_bytes = [int64]$_.Size;
           health_status = Text $_.HealthStatus }
    })
    $physical = @(Get-PhysicalDisk | ForEach-Object {
        @{ name = Text $_.FriendlyName; media_type = Text $_.MediaType;
           bus_type = Text $_.BusType; size_bytes = [int64]$_.Size;
           health_status = Text $_.HealthStatus }
    })
    $virtual = @(Get-VirtualDisk | ForEach-Object {
        @{ name = Text $_.FriendlyName; resiliency = Text $_.ResiliencySettingName;
           size_bytes = [int64]$_.Size; health_status = Text $_.HealthStatus }
    })
    $volumeAfterCandidates = @(Get-Volume -FilePath $mediaPath -ErrorAction Stop)
    if ($volumeAfterCandidates.Count -ne 1) { throw 'media volume changed during collection' }
    $volumeAfter = $volumeAfterCandidates[0]
    $volumeAfterIdentity = Text $volumeAfter.UniqueId
    $volumeAfterSizeBytes = [int64]$volumeAfter.Size
    $partitionsAfter = @(Get-Partition -Volume $volumeAfter -ErrorAction Stop)
    $diskNumbersAfter = @($partitionsAfter | Select-Object -ExpandProperty DiskNumber -Unique | Sort-Object { [int]$_ })
    if ($volumeAfterIdentity -ne $volumeIdentity -or
        $volumeAfterSizeBytes -ne $volumeSizeBytes -or
        ($diskNumbersAfter -join ',') -ne ($diskNumbers -join ',')) {
        throw 'media volume changed during collection'
    }
    $storage = @{ state = 'collected'; media_total_bytes = $volumeSizeBytes;
                  media_volume_backing_disks = $backing; media_volume_backing_disks_verified = $true;
                  physical_disks = $physical; virtual_disks = $virtual }
} catch {
}

$devices = @{ state = 'unavailable'; ups = @(); decklink = @(); blackmagic_drivers = @() }
try {
    $pnp = @(Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%DeckLink%' OR Name LIKE '%UPS%' OR Name LIKE '%Uninterruptible%' OR Manufacturer LIKE '%APC%' OR Manufacturer LIKE '%American Power Conversion%'" |
        ForEach-Object {
        @{ name = Text $_.Name; manufacturer = Text $_.Manufacturer }
    })
    $ups = @($pnp | Where-Object { ((Text $_.name) + ' ' + (Text $_.manufacturer)) -match '(?i)\bUPS\b|Uninterruptible|American Power Conversion|\bAPC\b' })
    $decklink = @($pnp | Where-Object { ((Text $_.name) + ' ' + (Text $_.manufacturer)) -match '(?i)DeckLink' })
    $drivers = @(Get-CimInstance Win32_PnPSignedDriver -Filter "DeviceName LIKE '%Blackmagic%' OR Manufacturer LIKE '%Blackmagic%' OR DeviceName LIKE '%DeckLink%'" |
        ForEach-Object {
        @{ name = Text $_.DeviceName; manufacturer = Text $_.Manufacturer;
           version = Text $_.DriverVersion }
    })
    $devices = @{ state = 'collected'; ups = $ups; decklink = $decklink;
                  blackmagic_drivers = $drivers }
} catch {
}

@{ collected_at = $now; gpu = $gpu; storage = $storage; devices = $devices } |
    ConvertTo-Json -Depth 8 -Compress
"#;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawInventory {
    collected_at: i64,
    gpu: RawGpuGroup,
    storage: RawStorageGroup,
    devices: RawDeviceGroup,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawGpuGroup {
    state: String,
    #[serde(default)]
    facts: Vec<RawGpuAdapter>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawStorageGroup {
    state: String,
    media_total_bytes: Option<u64>,
    #[serde(default)]
    media_volume_backing_disks: Vec<RawDisk>,
    #[serde(default)]
    media_volume_backing_disks_verified: bool,
    #[serde(default)]
    physical_disks: Vec<RawDisk>,
    #[serde(default)]
    virtual_disks: Vec<RawVirtualDisk>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDeviceGroup {
    state: String,
    #[serde(default)]
    ups: Vec<RawDevice>,
    #[serde(default)]
    decklink: Vec<RawDevice>,
    #[serde(default)]
    blackmagic_drivers: Vec<RawDriver>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawGpuAdapter {
    name: Option<String>,
    driver_version: Option<String>,
    vram_bytes: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDisk {
    name: Option<String>,
    media_type: Option<String>,
    bus_type: Option<String>,
    size_bytes: Option<i64>,
    health_status: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawVirtualDisk {
    name: Option<String>,
    resiliency: Option<String>,
    size_bytes: Option<i64>,
    health_status: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDevice {
    name: Option<String>,
    manufacturer: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDriver {
    name: Option<String>,
    manufacturer: Option<String>,
    version: Option<String>,
}

pub(crate) fn unavailable_evidence(collected_at_unix_ms: i64) -> MachineInventoryEvidence {
    MachineInventoryEvidence {
        collected_at_unix_ms,
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
    }
}

pub(crate) fn collect(media_path: &Path, collected_at_unix_ms: i64) -> MachineInventoryEvidence {
    let Some(system_root) = std::env::var_os("SystemRoot") else {
        return unavailable_evidence(collected_at_unix_ms);
    };
    let powershell = Path::new(&system_root)
        .join("System32")
        .join("WindowsPowerShell")
        .join("v1.0")
        .join("powershell.exe");
    if !powershell.is_file() {
        return unavailable_evidence(collected_at_unix_ms);
    }
    let mut command = hidden_command(&powershell);
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            WINDOWS_MACHINE_INVENTORY_SCRIPT,
        ])
        .env(MEDIA_PATH_ENV, media_path);
    let Ok(output) = run_bounded_command(command, COMMAND_TIMEOUT, MAX_OUTPUT_BYTES) else {
        return unavailable_evidence(collected_at_unix_ms);
    };
    if !output.success {
        return unavailable_evidence(collected_at_unix_ms);
    }
    parse_json(&output.stdout).unwrap_or_else(|_| unavailable_evidence(collected_at_unix_ms))
}

pub(crate) fn validate_or_unavailable(
    evidence: MachineInventoryEvidence,
    media_disk_free_gb: f64,
    probe_collected_at_unix_ms: i64,
    generated_at_unix_ms: i64,
) -> MachineInventoryEvidence {
    if evidence
        .validate_for_probe(
            media_disk_free_gb,
            probe_collected_at_unix_ms,
            generated_at_unix_ms,
        )
        .is_ok()
    {
        evidence
    } else {
        unavailable_evidence(probe_collected_at_unix_ms)
    }
}

fn parse_json(bytes: &[u8]) -> Result<MachineInventoryEvidence, String> {
    let raw: RawInventory = serde_json::from_slice(bytes)
        .map_err(|error| format!("machine inventory JSON is invalid: {error}"))?;
    if raw.collected_at < 0 {
        return Err("machine inventory collection time is invalid".into());
    }
    if (raw.gpu.state.trim() == "unavailable" && !raw.gpu.facts.is_empty())
        || (raw.storage.state.trim() == "unavailable"
            && (raw.storage.media_total_bytes.is_some()
                || !raw.storage.media_volume_backing_disks.is_empty()
                || raw.storage.media_volume_backing_disks_verified
                || !raw.storage.physical_disks.is_empty()
                || !raw.storage.virtual_disks.is_empty()))
        || (raw.devices.state.trim() == "unavailable"
            && (!raw.devices.ups.is_empty()
                || !raw.devices.decklink.is_empty()
                || !raw.devices.blackmagic_drivers.is_empty()))
    {
        return Err("unavailable machine inventory groups cannot carry facts".into());
    }
    Ok(MachineInventoryEvidence {
        collected_at_unix_ms: raw.collected_at,
        source: MachineEvidenceSource::WindowsCim,
        gpu_collection: parse_state(&raw.gpu.state)?,
        storage_collection: parse_state(&raw.storage.state)?,
        device_collection: parse_state(&raw.devices.state)?,
        media_volume_total_gb: bytes_to_gb(raw.storage.media_total_bytes, "media volume")?,
        media_volume_backing_disks: raw
            .storage
            .media_volume_backing_disks
            .into_iter()
            .map(normalize_media_volume_backing_disk)
            .collect::<Result<_, _>>()?,
        media_volume_backing_disks_verified: raw.storage.media_volume_backing_disks_verified,
        gpu_adapters: raw
            .gpu
            .facts
            .into_iter()
            .map(normalize_gpu)
            .collect::<Result<_, _>>()?,
        physical_disks: raw
            .storage
            .physical_disks
            .into_iter()
            .map(normalize_physical_disk)
            .collect::<Result<_, _>>()?,
        virtual_disks: raw
            .storage
            .virtual_disks
            .into_iter()
            .map(normalize_virtual_disk)
            .collect::<Result<_, _>>()?,
        ups_devices: raw
            .devices
            .ups
            .into_iter()
            .map(normalize_ups)
            .collect::<Result<_, _>>()?,
        decklink_devices: raw
            .devices
            .decklink
            .into_iter()
            .map(normalize_decklink)
            .collect::<Result<_, _>>()?,
        blackmagic_drivers: raw
            .devices
            .blackmagic_drivers
            .into_iter()
            .map(normalize_driver)
            .collect::<Result<_, _>>()?,
    })
}

fn parse_state(value: &str) -> Result<MachineInventoryCollectionState, String> {
    match value.trim() {
        "collected" => Ok(MachineInventoryCollectionState::Collected),
        "unavailable" => Ok(MachineInventoryCollectionState::Unavailable),
        _ => Err("machine inventory collection state is invalid".into()),
    }
}

fn text(value: Option<String>, field: &str) -> Result<String, String> {
    let value = value.unwrap_or_default().trim().to_string();
    if value.is_empty() || value.len() > 200 {
        return Err(format!("machine inventory {field} is invalid"));
    }
    Ok(value)
}

fn optional_text(value: Option<String>, field: &str) -> Result<Option<String>, String> {
    let value = value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if value.as_ref().is_some_and(|value| value.len() > 200) {
        return Err(format!("machine inventory {field} is invalid"));
    }
    Ok(value)
}

fn bytes_to_gb(value: Option<u64>, field: &str) -> Result<Option<f64>, String> {
    value
        .map(|value| {
            let gb = value as f64 / 1_073_741_824.0;
            if gb.is_finite() && gb > 0.0 {
                Ok(gb)
            } else {
                Err(format!("machine inventory {field} size is invalid"))
            }
        })
        .transpose()
}

fn signed_bytes_to_gb(value: Option<i64>, field: &str) -> Result<Option<f64>, String> {
    if value.is_some_and(|value| value < 0) {
        return Err(format!("machine inventory {field} size is invalid"));
    }
    bytes_to_gb(value.map(|value| value as u64), field)
}

fn normalize_gpu(raw: RawGpuAdapter) -> Result<GpuAdapterEvidence, String> {
    Ok(GpuAdapterEvidence {
        name: text(raw.name, "GPU name")?,
        adapter_memory_gb: match raw.vram_bytes {
            Some(0) | None => None,
            value => signed_bytes_to_gb(value, "GPU VRAM")?,
        },
        driver_version: optional_text(raw.driver_version, "GPU driver version")?,
    })
}

fn normalize_physical_disk(raw: RawDisk) -> Result<PhysicalDiskEvidence, String> {
    Ok(PhysicalDiskEvidence {
        name: text(raw.name, "physical disk name")?,
        size_gb: signed_bytes_to_gb(raw.size_bytes, "physical disk")?
            .ok_or_else(|| "machine inventory physical disk size is missing".to_string())?,
        media_type: optional_text(raw.media_type, "physical disk media type")?,
        bus_type: optional_text(raw.bus_type, "physical disk bus type")?,
        health_status: optional_text(raw.health_status, "physical disk health status")?,
    })
}

fn normalize_media_volume_backing_disk(
    raw: RawDisk,
) -> Result<MediaVolumeBackingDiskEvidence, String> {
    Ok(MediaVolumeBackingDiskEvidence {
        name: text(raw.name, "media-volume backing disk name")?,
        size_gb: signed_bytes_to_gb(raw.size_bytes, "media-volume backing disk")?
            .ok_or_else(|| "media-volume backing disk size is missing".to_string())?,
        media_type: optional_text(raw.media_type, "media-volume backing disk media type")?,
        bus_type: optional_text(raw.bus_type, "media-volume backing disk bus type")?,
        health_status: optional_text(raw.health_status, "media-volume backing disk health status")?,
    })
}

fn normalize_virtual_disk(raw: RawVirtualDisk) -> Result<VirtualDiskEvidence, String> {
    Ok(VirtualDiskEvidence {
        name: text(raw.name, "virtual disk name")?,
        size_gb: signed_bytes_to_gb(raw.size_bytes, "virtual disk")?
            .ok_or_else(|| "machine inventory virtual disk size is missing".to_string())?,
        resiliency: optional_text(raw.resiliency, "virtual disk resiliency")?,
        health_status: optional_text(raw.health_status, "virtual disk health status")?,
    })
}

fn normalize_ups(raw: RawDevice) -> Result<UpsDeviceEvidence, String> {
    Ok(UpsDeviceEvidence {
        name: text(raw.name, "device name")?,
        manufacturer: optional_text(raw.manufacturer, "UPS manufacturer")?,
    })
}

fn normalize_decklink(raw: RawDevice) -> Result<DeckLinkDeviceEvidence, String> {
    Ok(DeckLinkDeviceEvidence {
        name: text(raw.name, "device name")?,
        manufacturer: optional_text(raw.manufacturer, "DeckLink manufacturer")?,
    })
}

fn normalize_driver(raw: RawDriver) -> Result<BlackmagicDriverEvidence, String> {
    Ok(BlackmagicDriverEvidence {
        device_name: text(raw.name, "driver name")?,
        manufacturer: optional_text(raw.manufacturer, "Blackmagic driver manufacturer")?,
        driver_version: optional_text(raw.version, "Blackmagic driver version")?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn complete_json() -> Vec<u8> {
        br#"{"collected_at":123,"gpu":{"state":"collected","facts":[{"name":" RTX 4060 ","driver_version":" 1.2 ","vram_bytes":2147483648}]},"storage":{"state":"collected","media_total_bytes":107374182400,"media_volume_backing_disks":[{"name":" Media NVMe ","media_type":" SSD ","bus_type":" NVMe ","size_bytes":214748364800,"health_status":" Healthy "}],"media_volume_backing_disks_verified":true,"physical_disks":[{"name":" NVMe ","media_type":" SSD ","bus_type":" NVMe ","size_bytes":214748364800,"health_status":" Healthy "}],"virtual_disks":[{"name":" Mirror ","resiliency":" Mirror ","size_bytes":429496729600,"health_status":" Healthy "}]},"devices":{"state":"collected","ups":[{"name":" UPS ","manufacturer":" APC "}],"decklink":[{"name":" DeckLink ","manufacturer":" Blackmagic "}],"blackmagic_drivers":[{"name":" Desktop Video ","manufacturer":" Blackmagic ","version":" 14.0 "}]}}"#.to_vec()
    }

    #[test]
    fn complete_inventory_normalizes_and_converts_without_identifiers() {
        let evidence = parse_json(&complete_json()).unwrap();
        assert_eq!(evidence.collected_at_unix_ms, 123);
        assert_eq!(evidence.gpu_adapters[0].name, "RTX 4060");
        assert_eq!(evidence.gpu_adapters[0].adapter_memory_gb, Some(2.0));
        assert_eq!(evidence.media_volume_total_gb, Some(100.0));
        assert_eq!(evidence.media_volume_backing_disks[0].name, "Media NVMe");
        assert!(evidence.media_volume_backing_disks_verified);
        assert_eq!(evidence.physical_disks[0].name, "NVMe");
        assert_eq!(evidence.ups_devices[0].manufacturer.as_deref(), Some("APC"));
    }

    #[test]
    fn single_item_and_null_optional_facts_are_accepted() {
        let json = br#"{"collected_at":1,"gpu":{"state":"collected","facts":[{"name":"GPU","driver_version":null,"vram_bytes":null}]},"storage":{"state":"collected","media_total_bytes":null,"physical_disks":[],"virtual_disks":[]},"devices":{"state":"collected","ups":[],"decklink":[],"blackmagic_drivers":[]}}"#;
        let evidence = parse_json(json).unwrap();
        assert_eq!(evidence.gpu_adapters.len(), 1);
        assert_eq!(evidence.gpu_adapters[0].adapter_memory_gb, None);
        assert_eq!(evidence.media_volume_total_gb, None);
    }

    #[test]
    fn malformed_or_contradictory_domain_facts_fail_closed_at_collection_boundary() {
        assert!(parse_json(b"not json").is_err());
        let mut malformed = complete_json();
        malformed = String::from_utf8(malformed)
            .unwrap()
            .replace("\"collected\"", "\"future\"")
            .into_bytes();
        assert!(parse_json(&malformed).is_err());
        let negative = String::from_utf8(complete_json())
            .unwrap()
            .replace("2147483648", "-1")
            .into_bytes();
        assert!(parse_json(&negative).is_err());
        let unavailable_with_facts = String::from_utf8(complete_json())
            .unwrap()
            .replacen("\"state\":\"collected\"", "\"state\":\"unavailable\"", 1)
            .into_bytes();
        assert!(parse_json(&unavailable_with_facts).is_err());
        let invalid_backing_disk = String::from_utf8(complete_json())
            .unwrap()
            .replacen("\"size_bytes\":214748364800", "\"size_bytes\":-1", 1)
            .into_bytes();
        assert!(parse_json(&invalid_backing_disk).is_err());
        let unverified_relation = String::from_utf8(complete_json())
            .unwrap()
            .replace(
                "\"media_volume_backing_disks_verified\":true",
                "\"media_volume_backing_disks_verified\":false",
            )
            .into_bytes();
        assert!(parse_json(&unverified_relation).is_ok());
        let missing_verified_relation = parse_json(&unverified_relation).unwrap();
        assert!(
            missing_verified_relation
                .validate_for_probe(50.0, 123, 123)
                .is_err()
        );
    }

    #[test]
    fn parseable_but_invalid_inventory_degrades_to_unavailable_before_derivation() {
        let mut too_many = parse_json(&complete_json()).unwrap();
        too_many.collected_at_unix_ms = 100;
        too_many.gpu_adapters = (0..65)
            .map(|index| GpuAdapterEvidence {
                name: format!("GPU {index}"),
                adapter_memory_gb: Some(1.0),
                driver_version: None,
            })
            .collect();
        let degraded = validate_or_unavailable(too_many, 50.0, 100, 100);
        assert_eq!(
            degraded.gpu_collection,
            MachineInventoryCollectionState::Unavailable
        );
        assert!(degraded.gpu_adapters.is_empty());

        let mut oversized = parse_json(&complete_json()).unwrap();
        oversized.collected_at_unix_ms = 100;
        oversized.media_volume_total_gb = Some(f64::MAX);
        let degraded = validate_or_unavailable(oversized, 50.0, 100, 100);
        assert_eq!(
            degraded.storage_collection,
            MachineInventoryCollectionState::Unavailable
        );
        assert!(degraded.physical_disks.is_empty());
    }

    #[test]
    fn static_command_contract_cannot_be_http_controlled_or_leak_raw_identifiers() {
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("TOWNLIGHT_STATION_MEDIA_PROBE_PATH"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("Get-Volume -FilePath $mediaPath"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("Get-Partition -Volume $volume"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("Get-Disk -Number"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("volumeAfter"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("diskNumbersAfter"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("backingSizeBytes"));
        assert!(WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("media_volume_backing_disks"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("GetPathRoot"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("Win32_LogicalDisk"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("-DriveLetter"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("$args"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("DeviceID"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("SerialNumber"));
        assert!(!WINDOWS_MACHINE_INVENTORY_SCRIPT.contains("PNPDeviceID"));
    }

    #[cfg(windows)]
    #[test]
    #[ignore = "explicit Windows machine proof lane"]
    fn windows_machine_collector_proof_lane_returns_current_bounded_evidence() {
        let started_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;
        let evidence = collect(Path::new(env!("CARGO_MANIFEST_DIR")), started_at);
        assert_eq!(evidence.source, MachineEvidenceSource::WindowsCim);
        assert!(evidence.collected_at_unix_ms >= started_at);
        assert!(
            evidence.gpu_collection == MachineInventoryCollectionState::Collected
                || evidence.storage_collection == MachineInventoryCollectionState::Collected
                || evidence.device_collection == MachineInventoryCollectionState::Collected,
            "the installed proof machine returned no machine inventory evidence"
        );
        println!(
            "{}",
            serde_json::to_string_pretty(&evidence)
                .expect("serialize installed inventory evidence")
        );
    }
}
