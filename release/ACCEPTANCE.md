# Townlight Beta 1 — Station delivery update

This release distributes the exact signed candidate accepted in a fresh headless Windows Sandbox. Publication changes documentation and source integration refs; it does not rebuild, re-sign or replace the accepted installer bytes. Existing technical versions remain unchanged.

| Identity | Value |
|---|---|
| Coordinated label | Townlight Beta 1 |
| Release tag | `townlight-beta-1-station-delivery` |
| MSI product version | 1.6.0.1 |
| Station package version | 0.3.0 |
| MSI SHA-256 | `173c032b874e2cf4091cd79213f53c0663d272070fc11712701892d490e09883` |
| Exact source companion SHA-256 | `18d94a23b1a5d9f4ee6ecddb0e581f194c90ac8306b01b06d6534d86c2e2220d` |
| ProductCode | `{19333354-73F2-4925-B251-0BEB552D216B}` |
| Packaged/verified installed files | 23,716 |
| Signing | Valid Scott Converse publisher signature and Microsoft timestamp |

[Signing run](https://github.com/townlight/townlight/actions/runs/41163156492). `SIGNATURE-AND-CANDIDATE.json`, `SOURCE-REFS.json`, `STATION-DELIVERY-PROVENANCE.json` and `SHA256SUMS.txt` identify exact artifacts and integrated source refs. Integration tags are not represented as original build commits. The unchanged native desktop and 893 native payload files retain their accepted signatures/bytes.

## New installed acceptance

A fresh network-disabled Sandbox installed the signed MSI with exit0 and all23,716 installed hashes matched. Normal first-run used a fresh non-admin Windows account and empty profile. The installed UI created the synthetic city and administrator, completed setup/health and signed in. It created staff through the normal UI, authenticated staff and refused staff user administration. Native Meetings/Records commands created the synthetic meeting, assigned agenda and request.

The real installed Station operator and services passed all13 affected checks:

- Real native staff session grants operator ticket.
- Real headless operator live capture stop handoff logout.
- Separate CivicCast SRT adapter received live bytes before capture stop.
- Served recording hash equals completed output.
- Captured synthetic audio video independently decode.
- Normal Records request can read Station handoff.
- Repeated handoff retains one canonical document.
- Operator created explicit YouTube upload intent.
- Installed upload worker reused real YouTube client with synthetic Google acknowledgements.
- Exact packaged MP4 bytes sent with private visibility and reviewed title.
- Upload left canonical recording unchanged.
- Installed API prevents duplicate external upload.
- Station original stays locked and private.

The receiver was a separate process using the pinned CivicCast SRT source adapter. Its receipt was observed before capture stop; received media and the completed recording independently decoded. Two acknowledged Records handoffs retained one canonical hash-matched document, locked and private. The installed upload worker executed the actual pinned CivicCast OAuth/resumable client against synthetic Google acknowledgements; exact MP4 hash, reviewed title, private visibility and durable receipt matched. The original recording stayed unchanged; another upload was refused.

All owned media/service children terminated. Guest uninstall returned exit0, removed its application registration/executable/runtime, and the owned Sandbox stopped. Host application executable hashes/timestamps stayed unchanged. A managed initial registry view was empty; direct host registrations matched retained baseline and their key timestamps all predated the task. Original snapshots and full synthetic execution receipts are retained locally; this public report contains no profiles, credentials, tokens or city data.

## Retained baseline scope

The eight unchanged products retain the [original Beta 1 selected workflow and recovery acceptance](https://github.com/townlight/townlight/blob/townlight-beta-1/release/ACCEPTANCE.md). Core, Records, Meetings, Code, Zoning, Planning, Permitting and Inspections were not rerun wholesale for this Station-only update. Normal setup/staff access and the connected Meetings → Station → Records path were newly verified. Source-level interruption, permissions, uncertain-upload recovery, retry and disconnected-receiver checks also passed before packaging.

## Documentation and limits

All nine product manuals and the suite manual remain available in HTML/PDF/Word, with READMEs, product/organization landing pages, architecture drawings, exact API/MCP references, terms, index and full license. Affected Station/suite editions now document live CivicCast delivery and completed-recording YouTube uploads, configuration and recovery. Fourteen desktop/mobile page checks and headless Word/PDF rendering passed. The eight unchanged product manual bodies retain historical baseline artifact identities; the current versions page and this release identify the new MSI.

Development used synthetic accounts, records and media only. No new real YouTube upload, real city credentials, physical PEG/cable broadcast or historical in-place upgrade was tested. CivicCast's separately demonstrated real YouTube delivery remains separate evidence. Deployers supply Station-only OAuth credentials, SRT pairing/firewall settings and PEG operator configuration. Standalone CivicCast remains a separate product/repository/installation and was not changed. No new portal, universal vendor connectors, official fee engine or stack rewrite is part of this update.

Current Townlight-owned source and documentation are AGPL-3.0-only. Original vendor grants/notices are preserved. The source archive retains the exact prepublication build identity and private preparation notes; this report and publication tags identify its subsequent public delivery. A routine unsigned CI build is a different artifact and cannot inherit this MSI's signature or acceptance.
