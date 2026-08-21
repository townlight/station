# Managed CG image assets

TownLight Station treats a CG image as an immutable, content-addressed asset. The asset identity is the lowercase SHA-256 digest of the complete PNG or JPEG byte stream. Admission is bounded to 8 MiB and 4096×4096 pixels, and the pure-Rust decoder must decode the image to completion before it can become `ready`; malformed, animated PNG, unsupported, or mismatched content is rejected.

The managed path is derived from the exact canonical CG image root owned by `StationStore`:

`<cg-image-root>/<sha256>.png`

or `<sha256>.jpg` for JPEG. API, protocol, and database input never chooses this path. SQLite stores identity, format, dimensions, byte size, and readiness—not filesystem paths. Admission rejects reparse targets, writes a same-directory temporary file, flushes it, and atomically renames it to the immutable derived path.

Snapshot loading checks only board-referenced identities. It performs a cheap metadata check on every tick and re-reads, hashes, and fully decodes on first observation, file metadata change, authority change, or after the bounded 60-second verification interval. The verification cache is capped at 256 identities. Missing files become `Missing`; invalid, reparse, or hash-mismatched files become `Rejected` and resolve to typed placeholders.

The worker protocol carries only image identity and format, never a path. The worker derives the file beneath its trusted CG image root and independently checks containment, file type, byte identity, format, bounds, and complete decode immediately before apply. The persistent renderer uses two fixed image banks per physical slot so a valid PNG/JPEG can be prepared before the visible bank changes; an invalid update retains the prior valid pixels and snapshot. Live video, WPE/HTML content, background audio, and installed/field proof remain later gates.

The loopback API exposes `POST /api/v1/cg/images` for a raw PNG/JPEG body and `GET /api/v1/cg/images` for the currently verified managed library. The HTTP reader permits the 8 MiB body ceiling only on the exact upload route; unrelated routes retain their 64 KiB ceiling. The operator board designer uploads through a file picker, refreshes the verified library, automatically selects the admitted identity, and permits zone binding only to a currently ready bounded asset. Callers never provide a filesystem path or an identity.
