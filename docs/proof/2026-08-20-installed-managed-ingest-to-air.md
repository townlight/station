# Installed managed-ingest-to-air proof — 2026-08-20

## Candidate identity

- Source commit: `e331c9498e50a990dab376a2f50fcf52aac19057`
- Installer SHA-256: `7E69FE91462C2488C2B61CAAB0B68DBA322E31CB5AB368A166D05C981D53EA4A`
- Installed `stationd.exe` SHA-256: `E2B75E2A6CC6A24CA407C2F0FC52C7D605320B58B824B3C5AC864C08476ED754`
- Installed `channel-worker.exe` SHA-256: `6BCE4FE4892BC5127E1001395D96F6BDA0A46B55C49B57716BCAB32F1E17375F`
- Private media-runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The setup executable exited zero. The elevated installed-candidate verifier confirmed a running `LocalSystem` service, exact binary hashes, a ready private media runtime, the immutable source receipt above, and `{"database":"ready","status":"ready"}` from the installed loopback service. Existing station data survived the upgrade.

## Managed ingest

A finite 11.992-second MPEG-TS fixture containing real H.264 video and AAC audio was submitted only through `POST /api/v1/assets/ingest`. The request returned HTTP 202 and durable job `c0b6041b-8124-4094-90fa-a816b1a48ef3` progressed from `pending` to `completed` with one attempt.

The service independently measured the file, copied it into the station-owned library, revalidated the stored bytes, and committed ready asset `39f07788314d8bd53cc7d4d57d34a9e14af800a09ead5cdd6600980652dfdd6d`. Its managed path was beneath `%ProgramData%\TownLight Station\media`; the content identity exactly matched the source SHA-256. The readiness dashboard reported the asset `ready` and the ingest job `completed` with 100 percent progress.

The same dashboard also surfaced a deliberately retained historical asset whose file had been removed as `missing_file`. This proves that the dashboard checked disk state rather than trusting stale database readiness.

## Schedule-to-air execution

The proof then used only the installed API to:

1. Enable channel `27ab2e3f-a034-4844-97f2-eef251bef4b7` at UDP destination `127.0.0.1:5601`.
2. Create draft item `b1f98b00-20d7-444d-bc5e-a156be7e36a4` against the ingested asset.
3. Run plan `plan-7d8acd3e-7ba8-4be6-907e-5b6effa405db`, which passed with no conflicts, missing-media detail, or gaps.
4. Approve report `installed-19f9e315-60dc-4e6e-b98f-e7ab49441132` as `operator-scott`.
5. Observe the report lifecycle from the installed service:

```text
pending -> queued -> acknowledged -> completed
```

The worker durably loaded the exact content-identified library file before `queued`, took it at the committed start before `acknowledged`, returned to fallback after the five-second window before `completed`, and shut down after the proof channel was disabled. The service remained healthy.

The retained CRC-protected worker journal is `%ProgramData%\TownLight Station\channel-journals\27ab2e3f-a034-4844-97f2-eef251bef4b7.tlj`, SHA-256 `989182102E11ED54DE77231FD58266E17E81E6CD31F64EEC587E3A80833EC2F5`.

## Scope of this proof

This proves one exact installable candidate's managed-ingest, readiness, transactional scheduling, and acknowledged scheduled-air spine on this Windows machine. It does not replace the required seven-day one-station and seven-day three-station field gates, and it does not claim that the remaining media-library workflows or S1–S26 operator capabilities are complete.
