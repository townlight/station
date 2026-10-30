# Accepted Townlight Beta 1 Station boundary

The suite beta uses the already accepted CivicCast-based Python Station service,
React operator and pinned media engine under beta-1/. This boundary was selected
after the isolated proof, live capture and Meetings -> Station -> Records tests.
It reuses the suite's packaged Python runtime and retains independent Station
authority, PostgreSQL storage and owned media children. Native desktop remains
Tauri/Rust. No stack rewrite is part of this release.

The existing Rust kernel source is preserved as a separate development line;
its capability ledger and engineering assumptions are not claims about the
suite beta installer. The suite's installed acceptance and limitations are in
USER-MANUAL.md and the release acceptance information. Standalone CivicCast is
not modified, repackaged in place, auto-updated or given access to Townlight data.
The media source is an explicitly pinned copy with original grants/notices.

The owner expressly authorized redesign based on code and tests, then accepted
this isolated proof and production boundary. This records that selected boundary
without introducing any new beta capability or changing the separate kernel.
