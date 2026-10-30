# Third-party implementation boundary

Townlight Station's new application code is AGPL-3.0-only. The full license is in LICENSE.

Station invokes a separately provisioned, pinned CivicCast media implementation from
https://github.com/scottconverse/civiccast-native at commit
379d7bc95d3d46402a7d8fccd494ac2b1cb94154. CivicCast source notices remain intact,
including its Apache-2.0 code license and copyright notices.

Scope of that reuse: Station's own new modules (service, API, PostgreSQL job
ledger, recording store, operations, air/live switching, packaging and operator UI)
are AGPL-3.0-only and were written for this package; CivicCast is used as a pinned,
separately provisioned media component under its own Apache-2.0 terms. Scott owns
CivicCast, so that historical Apache-2.0 notice is preserved as an accurate record
of the component's published terms, not as a claim that Scott-owned code is
third-party code. Third-party components bundled by either project (GStreamer,
Python, FFmpeg, React and others) keep their own notices and are never relabeled.
This package does not rename or overwrite a standalone CivicCast installation and
does not relicense any component. media-lock.json records the exact reused Python
source hashes.

GStreamer, Python, FFmpeg and other dependencies retain their own licenses and
notices. Distribution packaging must retain the exact runtime's license BOM,
third-party notices, source/relinking materials where applicable and corresponding
source obligations. The private acceptance used the retained GStreamer 1.28.5 /
Python 3.12.10 pack; no new signed installer or public release has been built.

AGPL network source availability must point to the complete exact Station
distribution source, including modifications and build instructions. A release
cannot point only to an upstream CivicCast commit and omit Station modifications.

The operator UI uses React 19.1.1 and React DOM 19.1.1, built using Vite 7.3.7. Their MIT notices and the bundled scheduler notice are preserved in UI-THIRD-PARTY-LICENSES.txt. The UI source and lockfile are in operator-ui/.
