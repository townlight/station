#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Fail-closed installed-product GStreamer smoke evidence.

This module is intentionally reusable by GitHub Actions and a later Windows
Sandbox run.  It starts from an installed version root, never changes machine
PATH or registry state, and writes one hashable JSON result.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from civiccast.native.gstreamer_runtime import (
    bootstrap_installed_gstreamer_runtime,
    installed_gstreamer_environment,
)
from civiccast.native.runtime_closure import (
    FACTORY_PLUGIN,
    REQUIRED_FACTORIES,
    classify_missing_factories,
)


class InstalledGstreamerSmokeError(RuntimeError):
    """The installed product did not produce independently discoverable MPEG-TS."""


_DISCOVERER_LOG_LIMIT = 16 * 1024
# gst-discoverer 1.28.5 prints the topology container line in CAPS form
# ("container #0: video/mpegts, systemstream=(boolean)true,
# packetsize=(int)188"), not the human-readable codec name the original
# pattern expected -- reproduced byte-for-byte locally against the pinned
# 1.28.5 closure tools after candidate run 31198785853 failed on exactly
# this line (worker clean, real MPEG-TS bytes on disk). Both dialects are
# accepted; the caps form stays strict by requiring
# `systemstream=(boolean)true` on the same anchored line, so a non-TS
# container can never satisfy it.
_CONTAINER_RECORD = re.compile(
    r"^\s*container\s*(?:#\d+)?\s*:\s*(?:"
    r"(?:mpeg(?:-2)?\s+transport\s+stream|mpeg-ts)\s*$"
    r"|video/mpegts\b[^\r\n]*\bsystemstream=\(boolean\)true\b[^\r\n]*$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_VIDEO_CAPS_RECORD = re.compile(
    r"^\s*video\s*(?:#\d+)?\s*:\s*video/[^\r\n]+$", re.IGNORECASE | re.MULTILINE
)
_AUDIO_CAPS_RECORD = re.compile(
    r"^\s*audio\s*(?:#\d+)?\s*:\s*audio/[^\r\n]+$", re.IGNORECASE | re.MULTILINE
)
_NO_STREAMS_RECORD = re.compile(r"^\s*no streams found\.?\s*$", re.IGNORECASE | re.MULTILINE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def worker_command(*, python: Path, graph: Path) -> list[str]:
    return [str(python), "-m", "civiccast.egress.gst.worker", str(graph)]


def discoverer_command(*, version_root: Path, output: Path) -> list[str]:
    return [
        str(version_root / "dependencies/gstreamer/bin/gst-discoverer-1.0.exe"),
        "--verbose",
        str(output),
    ]


def _bounded_log(text: str) -> str:
    if len(text) <= _DISCOVERER_LOG_LIMIT:
        return text
    return text[:_DISCOVERER_LOG_LIMIT] + "\n[truncated]\n"


def _normalized_record(match: re.Match[str]) -> str:
    return " ".join(match.group(0).split())


def require_discoverer_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """Accept only anchored MPEG-TS, video-caps, and audio-caps records from stdout."""

    # Every refusal carries the bounded discoverer output. Candidate run
    # 31198785853 failed here with only "did not report an MPEG-TS
    # container" -- undiagnosable without the text the regex was run
    # against, the same silent-failure shape as the trust bridge (#371).
    def refused(reason: str) -> InstalledGstreamerSmokeError:
        return InstalledGstreamerSmokeError(
            f"{reason}\n--- gst-discoverer stdout ---\n{_bounded_log(result.stdout)}"
            f"\n--- gst-discoverer stderr ---\n{_bounded_log(result.stderr)}"
        )

    if result.returncode != 0:
        raise refused(f"gst-discoverer failed with {result.returncode}")
    if _NO_STREAMS_RECORD.search(result.stdout):
        raise refused("gst-discoverer explicitly reported no streams")
    container = _CONTAINER_RECORD.search(result.stdout)
    if container is None:
        raise refused("gst-discoverer did not report an MPEG-TS container")
    video = _VIDEO_CAPS_RECORD.search(result.stdout)
    if video is None:
        raise refused("gst-discoverer did not report a video caps record")
    audio = _AUDIO_CAPS_RECORD.search(result.stdout)
    if audio is None:
        raise refused("gst-discoverer did not report an audio caps record")
    return {
        "discovered_container_record": _normalized_record(container),
        "discovered_video_record": _normalized_record(video),
        "discovered_audio_record": _normalized_record(audio),
        "raw_discoverer_stdout": _bounded_log(result.stdout),
        "raw_discoverer_stderr": _bounded_log(result.stderr),
    }


def require_clean_worker_result(stdout: str) -> None:
    """Require the worker's own clean-result and teardown receipt, not just exit 0."""

    line = next((line for line in stdout.splitlines() if line.startswith("WORKER_RESULT ")), None)
    if line is None:
        raise InstalledGstreamerSmokeError("product worker emitted no WORKER_RESULT receipt")
    try:
        result = ast.literal_eval(line.removeprefix("WORKER_RESULT "))
    except (SyntaxError, ValueError) as exc:
        raise InstalledGstreamerSmokeError(
            "product worker emitted a malformed result receipt"
        ) from exc
    if (
        not isinstance(result, dict)
        or result.get("error") is not None
        or result.get("teardown_clean") is not True
    ):
        raise InstalledGstreamerSmokeError(
            f"product worker did not report a clean teardown: {result!r}"
        )


def _smoke_graph(output: Path) -> str:
    """Derive a short product graph using the normal graph serializer."""

    from civiccast.egress.gst.graph import (
        ElementSpec,
        PlayoutGraph,
        SourceLeg,
        audio_encode_specs,
        encode_chain_specs,
        graph_to_json,
    )

    def source(label: str, pattern: str) -> SourceLeg:
        return SourceLeg(
            label=label,
            elements=(ElementSpec("videotestsrc", props={"is-live": True, "pattern": pattern}),),
            audio=(ElementSpec("audiotestsrc", props={"is-live": True, "wave": "sine"}),),
        )

    graph = PlayoutGraph(
        sources=(source("installed-smoke-a", "smpte"), source("installed-smoke-b", "ball")),
        encoder=encode_chain_specs(width=320, height=180, fps=15, bitrate_kbps=500),
        audio_encoder=audio_encode_specs(bitrate_kbps=64),
        mux=ElementSpec("mpegtsmux"),
        sinks=((ElementSpec("queue"), ElementSpec("filesink", props={"location": str(output)})),),
    )
    return graph_to_json(graph)


def run_installed_smoke(*, version_root: Path, output: Path) -> dict[str, object]:
    """Run the worker and discoverer from the installed product tree."""

    root = version_root.resolve(strict=True)
    python = root / "python.exe"
    if not python.is_file():
        raise InstalledGstreamerSmokeError(f"installed app interpreter is missing: {python}")
    environment = installed_gstreamer_environment(root, base_environment=os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["SWAPS"] = "1"
    environment["INTERVAL"] = "1"
    graph_path = output.with_suffix(".graph.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(_smoke_graph(output), encoding="utf-8", newline="\n")

    # This is deliberately after the process-local bootstrap and before the
    # product worker invocation: no system GStreamer install can satisfy it.
    previous = dict(os.environ)
    try:
        os.environ.update(environment)
        bootstrap_installed_gstreamer_runtime()
        import gi  # type: ignore[import-not-found]

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore[import-not-found]

        Gst.init(None)
        missing = sorted(
            name for name in REQUIRED_FACTORIES if Gst.ElementFactory.find(name) is None
        )
        # Same rule the closure verifier applies (candidate run 31190955761:
        # this smoke hard-required d3d12h264dec/mfh265enc, which register
        # only on matching GPU/MFT hardware -- absent on hosted runners and
        # in Windows Sandbox). A hardware-gated name is excused ONLY when
        # its plugin FILE actually shipped; a missing DLL stays a genuine
        # failure. Excused names are reported, never silently dropped.
        plugin_dir = root / "dependencies/gstreamer/lib/gstreamer-1.0"
        plugin_file_missing = frozenset(
            name
            for name in missing
            if name in FACTORY_PLUGIN and not (plugin_dir / FACTORY_PLUGIN[name]).is_file()
        )
        hardware_gated_absent, genuine = classify_missing_factories(
            missing, plugin_file_missing=plugin_file_missing
        )
        if genuine:
            raise InstalledGstreamerSmokeError(
                f"installed GStreamer required factories are missing: {', '.join(genuine)}"
            )
    finally:
        os.environ.clear()
        os.environ.update(previous)

    worker = subprocess.run(  # noqa: S603 -- executable is resolved from verified version root
        worker_command(python=python, graph=graph_path),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if worker.returncode != 0:
        raise InstalledGstreamerSmokeError(
            f"product worker did not finish cleanly (exit {worker.returncode}): {worker.stdout}\n{worker.stderr}"
        )
    require_clean_worker_result(worker.stdout)
    if not output.is_file() or output.stat().st_size == 0:
        raise InstalledGstreamerSmokeError("product worker produced no MPEG-TS bytes")
    discovered = subprocess.run(  # noqa: S603 -- executable is verified app payload content
        discoverer_command(version_root=root, output=output),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    try:
        discoverer_evidence = require_discoverer_evidence(discovered)
    except InstalledGstreamerSmokeError as error:
        # Candidate run 31202293952: the discoverer said "Could not
        # determine type of stream" against a nonzero file, and nothing
        # recorded what the worker had actually written -- while the same
        # worker/graph produced a valid, discoverable TS locally. Append
        # byte-level forensics so the next such failure identifies the
        # file's real content instead of forcing another blind round trip.
        data = output.read_bytes()
        sync_positions = range(0, min(len(data), 188 * 16), 188)
        sync_ok = bool(data) and all(data[i] == 0x47 for i in sync_positions)
        raise InstalledGstreamerSmokeError(
            f"{error}\n--- output file forensics ---\n"
            f"bytes={len(data)} first32={data[:32].hex()} "
            f"ts_sync_first_16_packets={'ok' if sync_ok else 'BROKEN'}"
        ) from error
    return {
        "status": "PASS",
        "version_root": str(root),
        "output": str(output),
        "output_bytes": output.stat().st_size,
        "output_sha256": _sha256(output),
        "worker_returncode": worker.returncode,
        "worker_result_seen": True,
        "required_factory_count": len(REQUIRED_FACTORIES),
        "hardware_gated_absent": list(hardware_gated_absent),
        **discoverer_evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report: dict[str, Any] = run_installed_smoke(
            version_root=args.version_root, output=args.output.resolve()
        )
    except (InstalledGstreamerSmokeError, OSError, subprocess.SubprocessError) as exc:
        report = {"status": "FAIL", "error": str(exc)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
