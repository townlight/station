# TSDuck 3.44 contract fixture

`tsduck-3.44-h264-aac.ts` is a two-second deterministic H.264/AAC MPEG-TS
fixture (SHA-256 `972b68d6b6e5ee85eb23812d173572f9d0f4fcd00d8514a72119391e46cc8f95`).
It was generated once for tests with the explicitly authorized fixture-only
FFmpeg binary SHA-256 `57c56e369d5b4873b4d93fc1a1d833cb7cd8bc9325c14b05c34ce60b22842d8a`.
The retained analyzer and tables output came from the pinned TSDuck archive SHA-256
`b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1`.

The analyzer output intentionally retains the bounded TSDuck error-analysis
preamble before the JSON object. Missing BAT/TDT/TOT are normal for this minimal
FFmpeg mux and are not treated as a passing claim about required program streams.
