# Shared meeting imports

Use **Meetings → Import meeting records** in Windows 1.8.1.1. Select a
source and format, enter the original public URL, then choose **Preview import**.
For a saved response, select the file before previewing. Choose a meeting and
use **Save selected meeting**. Townlight records the source URL, format, stable
source ID and capture hash with the canonical meeting. Reopening the same
capture returns the existing meeting. A changed capture for that identity is
reported for staff review.

The source and format selector lists the supported reader pairs below. A
public URL must serve the chosen format directly. Use a saved public response
for POST-only listings, portals that require a browser session and sources
whose page does not return the required response. The importer reads what a
municipality publishes in that response. Fields and attachments vary by
municipality and by meeting.

| Source | Input to preview |
|---|---|
| Legistar | Core-format JSON export |
| PrimeGov | Core-format JSON export or public portal meeting list JSON |
| Granicus | Core-format JSON export or public ViewPublisher HTML |
| NovusAGENDA | Core-format JSON export |
| CivicPlus Agenda and Meeting Management Select | Public Events JSON |
| CivicPlus Agenda Center | Public listing HTML |
| Municode Meetings | Public meetings HTML |
| BoardBook | Public organization HTML |
| eSCRIBE | Public calendar JSON |
| Simbli | Public meeting listing HTML |
| iCompass CivicWeb | Public meeting-type listing HTML |
| iQM2 | Public meeting list HTML |
| Granicus Swagit | Public video archive HTML |
| SuiteOne Media | Public meeting archive HTML |
| Granicus Peak | Public agenda center ViewPublisher HTML |
| Utah Public Notice Website | Public notice detail HTML |
| Destiny AgendaQuick | Public meeting list HTML |
| Town Hall Streams | Public archive HTML |
| TelVue | Public playlist HTML |
| CHAMP | Public event JSON |
| Hyland OnBase | Public Agenda Online HTML |
| Laserfiche WebLink | Saved public dated document folder HTML |
| TownCloud | Saved public agenda table JSON or public agenda detail HTML |
| Digital Towpath | Public meeting detail HTML |
| ClerkBase | Public meeting document HTML |
| Municipality-published calendar | Public iCalendar feed |
| Wistia | Public media JSON |
| Boardable | Public Site meeting detail HTML |
| gWorks FrontDesk | Public city meetings JSON |
| Diligent BoardDocs | Unencrypted printed agenda PDF with numbered items |
| Tyler Meeting Manager | Saved public calendar JSON. Meeting information and item titles when present. |

## Staff handoffs

- **Core and Meetings.** Core's existing vendor formats share the same preview
  and save path as the public readers. The saved meeting and any structured
  agenda items open in the regular Meetings workflow.
- **Records.** In Meetings, load imported documents by meeting ID, choose a
  PDF and enter the destination Records request ID. The attachment retains
  the citation and file hash. An HTML viewer or a dead document link may need
  staff to obtain the PDF separately.
- **Notice and Access.** Use **Review imported text** to start a Notice draft
  or accessibility review with the source citation. Use each workspace's
  normal review and save actions.
- **Station.** In the configured Station operator page, select the saved
  meeting and stage a direct MP4. Use the private capture workflow to queue
  processing. Player-only references are visible without a staging action.
- **Code.** Use the existing ordinance proposal and adoption workflow with
  the canonical imported meeting and agenda IDs.
- **Boards, Contracts, Procurement, Zoning, Planning, Permitting and
  Inspections.** In the destination workspace use **Imported meeting
  evidence**. Select an existing staff record and the saved meeting, then
  save the citation. The destination's existing role and workflow apply.

## Shared interface

The installed Townlight Python runtime exposes a read-only CLI. CivicCast and
other local clients can consume its normalized JSON and apply their own access
and persistence rules.

```powershell
python -m townlight_runtime.meeting_imports champ PUBLIC_EVENT_URL saved-event.json --format champ-event-json
```

Use `--fetch` in place of the saved file to read a supported public response.

## Interfaces and other products

The version-1 normalized JSON contract is shared by the Python function
`townlight_runtime.meeting_imports.preview_export(connector, source_url, content, input_format=...)`
and the CLI above. A saved response can also be read from stdin using `-`.
Inputs are limited to 16 MiB. Preview is read-only; the consuming application
owns persistence, staff authorization and its own data.

The installed native HTTP bridge exposes `GET /desktop/v1/meetings/imports/sources`,
`POST /desktop/v1/meetings/imports/preview`, `POST /desktop/v1/meetings/imports/fetch`,
`POST /desktop/v1/meetings/imports/commit`, and meeting/link/document handoff
operations in the [native API schema](https://townlight.github.io/api/suite-openapi.json).
Use the existing `X-Townlight-Session` authentication, mapped staff roles and
configured local PostgreSQL service. This is a native application interface;
there is no independent anonymous importer HTTP service or importer MCP server.

**CivicCast:** opt in through its Townlight shared importer source setting and
`TOWNLIGHT_IMPORT_PYTHON` (the absolute Python executable), `TOWNLIGHT_IMPORT_CONNECTOR`,
`TOWNLIGHT_IMPORT_FORMAT`, `TOWNLIGHT_IMPORT_URL` and optional `TOWNLIGHT_IMPORT_CAPTURE`.
The adapter saves normalized items through the existing AgendaStore. Structured
agenda availability depends on the source response; a video listing can contain
meeting metadata without agenda items. Existing sources remain the default.

**CivicCast Brief:** its existing PrimeGov and Granicus discovery routes can opt
into the shared reader using `TOWNLIGHT_IMPORT_PYTHON`. Brief does not expose all
31 sources as a new selection menu. Its existing caption/media workflow remains
the consuming path. See each project's shared-import client documentation.

**TownReporter, Describer Workbench and Textflowkit:** no shared-import adapter
was added to these products. TownReporter retains its own PrimeGov path.
Describer Workbench and Textflowkit expose their existing transcription APIs/MCP
interfaces, which do not constitute meeting-import integration. The shared engine
ships inside Townlight Runtime; a standalone library is outside this release.

## Windows acceptance and limits

Windows 1.8.1.1 adds shared meeting imports for 31 source families, with 34 supported source/format pairs. Installed Windows Sandbox checks exercised preview, save and durable readback using public captures and fixtures, plus the documented Townlight handoffs. These checks do not establish automatic discovery on every live portal or a complete retest of every module.

Windows 1.8.1.1 has fresh-install acceptance. An in-place upgrade to 1.8.1.1 was not exercised. The previous Windows 1.8.0.1 release tested upgrade from 1.7.2.1; that result applies to 1.8.0.1. Before an existing-city evaluation, back up city data and settings, close Townlight, keep the existing %LOCALAPPDATA%\CivicSuite profile, and use the same Windows account. Keep the prior installer and a restore path. Station continues to use its separately configured service.

The accepted MSI contains Townlight Runtime 0.3.1 and Meetings 1.0.6 distributions. Its health version strings still report Runtime 0.3.0 and Meetings 1.0.5. Default-branch source corrects those display strings; the signed MSI and its exact build source remain unchanged.

[Exact Windows 1.8.1.1 acceptance and limits](https://github.com/townlight/townlight/releases/download/townlight-windows-v1.8.1.1/ACCEPTANCE-Windows-1.8.1.1.md).
