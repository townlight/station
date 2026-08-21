use std::net::TcpListener;
use std::path::PathBuf;

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let database = arguments
        .next()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("station.db"));
    let address = arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .unwrap_or_else(|| "127.0.0.1:4070".to_string());

    let listener = TcpListener::bind(&address).unwrap_or_else(|error| {
        eprintln!("TownLight Station could not listen on {address}: {error}");
        std::process::exit(1);
    });
    println!("TownLight Station is listening on http://{address}");
    if let Err(error) = townlight_station::serve(listener, database) {
        eprintln!("TownLight Station stopped: {error}");
        std::process::exit(1);
    }
}
