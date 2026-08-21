# ADR 0005: Native Windows service lifecycle

Date: 2026-08-20

Status: Accepted

## Context

An operator-installed broadcast appliance must start without an interactive login, expose truthful state to Windows, store data at an installer-selected durable location, and shut down without abandoning an accepted API request. Running a console program from a shortcut or background shell does not provide those guarantees.

## Decision

`stationd` has distinct console and native service launch modes.

- Windows Service Control Manager starts `stationd service` under the fixed internal name `TownLightStation`.
- The installer supplies an absolute database path and a loopback-only HTTP address. Service mode rejects weaker configuration before dispatch.
- The service reports start-pending before binding, running only after the data directory and listener are ready, stop-pending when a stop or system-shutdown control arrives, and stopped after the API accept loop exits.
- The HTTP listener stops accepting new connections after the cooperative signal. A connection already accepted may finish within its bounded five-second read deadline; the SCM stop wait hint is seven seconds.
- Only the listener is nonblocking so it can observe the cooperative stop signal. Every accepted Windows socket is explicitly returned to blocking mode before applying the five-second request deadline; otherwise a client pause between connect and send is misread as an immediate timeout.
- Startup failures produce both a service-specific stopped status and a local `stationd-startup-error.txt` receipt beside the database.
- Console mode remains available for development and preserves its existing positional arguments.

## Evidence

The optimized binary was registered temporarily with Windows Service Control Manager as LocalSystem on a Windows 11 development machine. It reached `RUNNING`, answered `GET /health` with HTTP 200 and `{"database":"ready","status":"ready"}`, created the configured database, accepted a stop, reached `STOPPED`, and was removed. The cooperative API boundary and service configuration parser also have behavioral tests.

## Consequences

Installer work now has a real service entry point rather than a packaging assumption. The service is intentionally local-only; browser access outside the station requires a separately designed authenticated boundary. Recovery policy, delayed automatic start, service ACLs, upgrade orchestration, and installed-machine rollback receipts remain installer responsibilities.
