use std::ffi::OsString;
use std::net::SocketAddr;
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchMode {
    Console,
    Service,
    AuthIssueBootstrapHandoff { handoff: PathBuf },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaemonConfig {
    pub mode: LaunchMode,
    pub database: PathBuf,
    pub address: SocketAddr,
}

pub fn parse(arguments: impl IntoIterator<Item = OsString>) -> Result<DaemonConfig, String> {
    let mut arguments = arguments.into_iter().peekable();
    if arguments.peek().and_then(|value| value.to_str()) == Some("auth") {
        arguments.next();
        return parse_auth(arguments);
    }
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
    if !address.ip().is_loopback() {
        return Err("station control address must remain on loopback".into());
    }
    Ok(DaemonConfig {
        mode,
        database,
        address,
    })
}

fn parse_auth(mut arguments: impl Iterator<Item = OsString>) -> Result<DaemonConfig, String> {
    if arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .as_deref()
        != Some("issue-bootstrap-handoff")
    {
        return Err("auth requires the issue-bootstrap-handoff command".into());
    }

    let mut database = None;
    let mut handoff = None;
    while let Some(argument) = arguments.next() {
        match argument.to_str() {
            Some("--database") if database.is_none() => {
                database = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or("bootstrap handoff --database requires a path")?,
                ));
            }
            Some("--handoff") if handoff.is_none() => {
                handoff = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or("bootstrap handoff --handoff requires a path")?,
                ));
            }
            Some("--database") => {
                return Err("bootstrap handoff --database may be specified only once".into());
            }
            Some("--handoff") => {
                return Err("bootstrap handoff --handoff may be specified only once".into());
            }
            _ => {
                return Err("bootstrap handoff mode accepts only --database and --handoff".into());
            }
        }
    }

    let database = database.ok_or("bootstrap handoff requires --database")?;
    let handoff = handoff.ok_or("bootstrap handoff requires --handoff")?;
    if !database.is_absolute() || !handoff.is_absolute() {
        return Err("bootstrap handoff requires absolute --database and --handoff paths".into());
    }
    Ok(DaemonConfig {
        mode: LaunchMode::AuthIssueBootstrapHandoff { handoff },
        database,
        address: "127.0.0.1:4070".parse().expect("default address is valid"),
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

    #[test]
    fn rejects_a_non_loopback_console_address() {
        let error = parse(["custom.db", "0.0.0.0:4800"].map(OsString::from)).unwrap_err();
        assert!(error.contains("loopback"), "unexpected error: {error}");
    }

    #[test]
    fn parses_only_absolute_paths_for_bootstrap_handoff_mode() {
        let result = parse(
            [
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\ProgramData\TownLight Station\station.db",
                "--handoff",
                r"C:\ProgramData\TownLight Station\bootstrap.capability",
            ]
            .map(OsString::from),
        );

        let config = result.unwrap();
        assert_eq!(
            config.database,
            PathBuf::from(r"C:\ProgramData\TownLight Station\station.db")
        );
        assert_eq!(
            config.mode,
            LaunchMode::AuthIssueBootstrapHandoff {
                handoff: PathBuf::from(r"C:\ProgramData\TownLight Station\bootstrap.capability")
            }
        );
    }

    #[test]
    fn bootstrap_handoff_mode_rejects_network_relative_or_extra_arguments() {
        for arguments in [
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                "station.db",
                "--handoff",
                r"C:\bootstrap.capability",
            ],
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\station.db",
                "--handoff",
                "bootstrap.capability",
            ],
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\station.db",
                "--handoff",
                r"C:\bootstrap.capability",
                "--address",
                "127.0.0.1:4070",
            ],
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\station.db",
                "--handoff",
                r"C:\bootstrap.capability",
                "unexpected",
            ],
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\station.db",
            ],
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--handoff",
                r"C:\bootstrap.capability",
            ],
            vec![
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\station.db",
                "--database",
                r"C:\other.db",
                "--handoff",
                r"C:\bootstrap.capability",
            ],
        ] {
            let error = parse(arguments.into_iter().map(OsString::from)).unwrap_err();
            assert!(
                error.contains("bootstrap handoff"),
                "unexpected error: {error}"
            );
        }
    }

    #[test]
    fn bootstrap_handoff_mode_never_accepts_or_echoes_an_argv_secret() {
        let secret = "must-not-appear-in-errors";
        let error = parse(
            [
                "auth",
                "issue-bootstrap-handoff",
                "--database",
                r"C:\station.db",
                "--handoff",
                r"C:\bootstrap.capability",
                "--token",
                secret,
            ]
            .map(OsString::from),
        )
        .unwrap_err();

        assert!(!error.contains(secret), "argv secret was echoed: {error}");
    }
}
