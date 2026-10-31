# Townlight Beta 1 private Station delivery candidate

AGPL-3.0-only for Townlight-owned source; preserve vendor grants. This archive updates the previous accepted beta source with the exact shipped Station Python/UI and its full package build sources. Standalone CivicCast is separate.

Build station/pyproject.toml as a pure-Python wheel using the existing locked environment; operator-ui/package-lock.json/build.mjs rebuilds its bundled React UI. Stage only the Station package/metadata while preserving signed native entrypoints. The station-delivery-build recipes record the task layout used for this candidate; replace task roots explicitly when reconstructing. prepare_wix preserves original MSI components and adds only delivery.py, feed_job.py and the delivery guide. Exclude regenerable bytecode. Reuse native inputs identified by runtime-payload-lock.json and the prior signing provenance. Copy the unchanged installer icon resource before linking.

Existing component versions remain unchanged. The new MSI must be signed as its own exact prebuilt candidate using the established Townlight sign-only GitHub workflow; nothing is published by this archive. Validate signatures/hash mappings and a fresh headless synthetic Sandbox installation. Updated documentation is a separate companion; city-specific OAuth/SRT credentials are never included.


The accepted source/build archive records the original private preparation. Publishing the exact MSI does not rebuild or resign it. Use the Station beta-1/ implementation; its media-source directory is a pinned donor, and standalone CivicCast is a separate product.
