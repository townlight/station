use std::ffi::OsString;
use std::net::SocketAddr;
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchMode {
    Console,
    Service,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonConfig {
    pub mode: LaunchMode,
    pub database: PathBuf,
    pub address: SocketAddr,
}

pub fn parse(arguments: impl IntoIterator<Item = OsString>) -> Result<DaemonConfig, String> {
    let mut arguments = arguments.into_iter().peekable();
    let mode = match arguments.peek().and_then(|value| value.to_str()) {
        Some("service") => {
            arguments.next();
            LaunchMode::Service
        }
        Some("console") => {
            arguments.next();
            LaunchMode::Console
        }
        _ => LaunchMode::Console,
    };
    let mut database = None;
    let mut address: Option<SocketAddr> = None;
    let mut positional = Vec::new();

    while let Some(argument) = arguments.next() {
        match argument.to_str() {
            Some("--database") => {
                if database.is_some() {
                    return Err("--database may be specified only once".into());
                }
                database = Some(PathBuf::from(
                    arguments.next().ok_or("--database requires a path")?,
                ));
            }
            Some("--address") => {
                if address.is_some() {
                    return Err("--address may be specified only once".into());
                }
                let value = arguments
                    .next()
                    .and_then(|value| value.into_string().ok())
                    .ok_or("--address requires a UTF-8 socket address")?;
                address = Some(
                    value
                        .parse()
                        .map_err(|_| "--address must be an IP address and port")?,
                );
            }
            Some(value) if value.starts_with('-') => {
                return Err(format!("unknown stationd option: {value}"));
            }
            _ if mode == LaunchMode::Service => {
                return Err("service mode accepts only --database and --address".into());
            }
            _ => positional.push(argument),
        }
    }

    if !positional.is_empty() {
        if database.is_some() || address.is_some() || positional.len() > 2 {
            return Err("console mode accepts [database] [address] or named options".into());
        }
        database = Some(PathBuf::from(positional.remove(0)));
        if let Some(value) = positional.pop() {
            let value = value.into_string().map_err(|_| "address must be UTF-8")?;
            address = Some(
                value
                    .parse()
                    .map_err(|_| "address must be an IP address and port")?,
            );
        }
    }

    let database = database.unwrap_or_else(|| PathBuf::from("station.db"));
    let address =
        address.unwrap_or_else(|| "127.0.0.1:4070".parse().expect("default address is valid"));
    if mode == LaunchMode::Service && !database.is_absolute() {
        return Err("service mode requires an absolute --database path".into());
    }
    if mode == LaunchMode::Service && !address.ip().is_loopback() {
        return Err("service mode address must remain on loopback".into());
    }
    Ok(DaemonConfig {
        mode,
        database,
        address,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_an_absolute_service_configuration() {
        let config = parse(
            [
                "service",
                "--database",
                r"C:\ProgramData\TownLight Station\station.db",
                "--address",
                "127.0.0.1:4700",
            ]
            .map(OsString::from),
        )
        .unwrap();

        assert_eq!(config.mode, LaunchMode::Service);
        assert_eq!(
            config.database,
            PathBuf::from(r"C:\ProgramData\TownLight Station\station.db")
        );
        assert_eq!(config.address, "127.0.0.1:4700".parse().unwrap());
    }

    #[test]
    fn rejects_a_relative_database_for_service_mode() {
        let error = parse(["service", "--database", "station.db"].map(OsString::from)).unwrap_err();
        assert!(error.contains("absolute"), "unexpected error: {error}");
    }

    #[test]
    fn rejects_a_non_loopback_service_address() {
        let error = parse(
            [
                "service",
                "--database",
                r"C:\ProgramData\TownLight Station\station.db",
                "--address",
                "0.0.0.0:4070",
            ]
            .map(OsString::from),
        )
        .unwrap_err();
        assert!(error.contains("loopback"), "unexpected error: {error}");
    }

    #[test]
    fn preserves_the_existing_console_arguments() {
        let config = parse(["custom.db", "127.0.0.1:4800"].map(OsString::from)).unwrap();
        assert_eq!(config.mode, LaunchMode::Console);
        assert_eq!(config.database, PathBuf::from("custom.db"));
        assert_eq!(config.address, "127.0.0.1:4800".parse().unwrap());
    }
}
