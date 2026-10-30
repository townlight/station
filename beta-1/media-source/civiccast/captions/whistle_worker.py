# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Private serial speech child. Parent applies Windows limits before resume."""

from __future__ import annotations

import array
import base64
import contextlib
import hashlib
import importlib
import json
import os
import sys
from typing import Any, cast


def main() -> None:
    from pathlib import Path

    from civiccast.captions.models import AudioChunk, CustomVocabulary
    from civiccast.captions.whistle import LIBRARY_SHA256, WEIGHTS_SHA256

    engine_name, weights, library, config = sys.argv[1:]
    output = sys.stdout
    os.environ.update(
        NEEDLE_TELEMETRY="0", DO_NOT_TRACK="1", HF_HUB_OFFLINE="1", OMP_NUM_THREADS="1"
    )
    with contextlib.redirect_stdout(sys.stderr):
        if engine_name == "whistle":
            from importlib.metadata import version

            if version("cactus-needle") != "3.1.0":
                raise RuntimeError("Whistle requires pinned cactus-needle 3.1.0")
            for path, digest in ((weights, WEIGHTS_SHA256), (library, LIBRARY_SHA256)):
                if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
                    raise RuntimeError("Whistle asset hash mismatch")
            os.environ["NEEDLE3_LIB_PATH"] = library
            # cactus-needle ships as a pinned binary package without inline stubs.
            needle = cast(Any, importlib.import_module("needle"))
            telemetry = cast(Any, importlib.import_module("needle._telemetry"))

            if telemetry._enabled():
                raise RuntimeError("Needle telemetry must be disabled")

            engine = needle.Whistle(weights=weights)
            device = "cpu"
        elif engine_name == "whisper":
            from civiccast.captions.runtime import FasterWhisperRuntime

            engine = FasterWhisperRuntime(**json.loads(config))
            engine.prepare()
            device = "cuda" if engine.on_cuda() else "cpu"
        else:
            raise ValueError("unknown speech child engine")
    print(json.dumps({"ready": engine_name, "device": device}), file=output, flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            chunk = AudioChunk(
                **request["chunk"], pcm_s16le=base64.b64decode(request["pcm"], validate=True)
            )
            vocabulary = (
                CustomVocabulary.model_validate(request["vocabulary"])
                if request["vocabulary"]
                else None
            )
            with contextlib.redirect_stdout(sys.stderr):
                if engine_name == "whistle":
                    pcm = array.array("h")
                    pcm.frombytes(chunk.pcm_s16le)
                    result = engine.transcribe(
                        array.array("f", (x / 32768 for x in pcm)),
                        language="en",
                        word_timestamps=True,
                        keywords=vocabulary.terms if vocabulary else None,
                    )
                else:
                    result = [r.model_dump() for r in engine.transcribe([chunk], vocabulary)]
            response = {"id": request["id"], "result": result}
        except Exception as exc:
            response = {"id": request["id"], "error": str(exc)}
        print(json.dumps(response), file=output, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        raise SystemExit(1) from exc
