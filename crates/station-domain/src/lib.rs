use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StationProfile {
    pub station_id: String,
    pub display_name: String,
    pub timezone: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StationProfileDocument {
    #[serde(flatten)]
    pub profile: StationProfile,
    pub revision: u64,
}

impl StationProfile {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.station_id) {
            return Err("station_id must be a canonical UUID.");
        }
        let display_name = self.display_name.trim();
        if display_name.is_empty()
            || display_name.len() > 120
            || display_name.chars().any(char::is_control)
        {
            return Err("display_name must contain 1 to 120 visible characters.");
        }
        if !is_iana_timezone(&self.timezone) {
            return Err("timezone must be an IANA timezone such as America/Denver.");
        }
        Ok(())
    }
}

fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}

fn is_iana_timezone(value: &str) -> bool {
    value == "UTC"
        || (value.contains('/')
            && value.split('/').all(|part| {
                !part.is_empty()
                    && part.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'+')
                    })
            }))
}
