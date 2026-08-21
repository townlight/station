use std::ffi::OsString;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaunchMode {
    Service,
    Once,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdaterConfig {
    pub mode: LaunchMode,
    pub database: PathBuf,
    pub application_root: PathBuf,
}

pub fn parse(arguments: impl IntoIterator<Item = OsString>) -> Result<UpdaterConfig, String> {
    let mut arguments = arguments.into_iter();
    let mode = match arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .as_deref()
    {
        Some("service") => LaunchMode::Service,
        Some("once") => LaunchMode::Once,
        _ => {
            return Err(
                "usage: station-updater (service|once) --database PATH --application-root PATH"
                    .into(),
            );
        }
    };
    let mut database = None;
    let mut application_root = None;
    while let Some(argument) = arguments.next() {
        match argument.to_str() {
            Some("--database") if database.is_none() => {
                database = Some(PathBuf::from(
                    arguments.next().ok_or("--database requires a path")?,
                ));
            }
            Some("--application-root") if application_root.is_none() => {
                application_root = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or("--application-root requires a path")?,
                ));
            }
            Some(value) => {
                return Err(format!(
                    "unknown or repeated station-updater option: {value}"
                ));
            }
            None => return Err("station-updater options must be Unicode".into()),
        }
    }
    let database = database.ok_or("--database is required")?;
    let application_root = application_root.ok_or("--application-root is required")?;
    if !database.is_absolute() || !application_root.is_absolute() {
        return Err("station-updater paths must be absolute".into());
    }
    Ok(UpdaterConfig {
        mode,
        database,
        application_root,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_explicit_absolute_service_paths() {
        let parsed = parse(
            [
                "service",
                "--database",
                r"C:\ProgramData\TownLight Station\station.db",
                "--application-root",
                r"C:\Program Files\TownLight Station",
            ]
            .map(OsString::from),
        )
        .unwrap();
        assert_eq!(parsed.mode, LaunchMode::Service);
        assert!(parse(["once", "--database", "station.db"].map(OsString::from)).is_err());
    }
}
