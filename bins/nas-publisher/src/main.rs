fn main() {
    if std::env::args_os().nth(1).as_deref() != Some(std::ffi::OsStr::new("--nas-publisher-worker"))
    {
        eprintln!("NAS publisher worker requires its private worker switch");
        std::process::exit(2);
    }
    if let Err(error) = station_storage::run_local_nas_publisher_worker_from_env() {
        eprintln!("NAS publisher worker failed: {error}");
        std::process::exit(1);
    }
}
