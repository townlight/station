# Changelog

All notable changes to TownLight Station will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Fresh TownLight Station repository and native Rust control-plane foundation.
- Versioned station-profile read/write API with typed JSON errors.
- SQLite WAL persistence using the database runtime included with Windows.
- Validation for station identity, operator-visible name, and IANA timezone.
- HTTP health endpoint and end-to-end transport tests.
