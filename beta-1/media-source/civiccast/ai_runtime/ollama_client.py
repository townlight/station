# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Small stdlib Ollama client used by release runtime adapters."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# The control-plane socket budget for a live generate call, distinct from the
# short budget used for cheap /api/tags and /api/version calls (DEFAULT_TIMEOUT_
# SECONDS below). Field evidence (candidate #17, CPU-only 32GB reference
# hardware): the old blanket 120s timeout killed the HTTP client's socket read
# while ollama was still generating -- ollama itself went on to return 200 to a
# client that had already disconnected (POST /completion succeeded server-side;
# the control plane 503'd at ~120s regardless). Measured on the same hardware
# class: gemma4:e4b completed in 94-128s (already tight against 120s);
# gemma4:12b took up to 366s. 600s gives real local CPU generation room to
# finish and return its result rather than being discarded after the model did
# the work. This is the socket-level fix; SummaryGenerationJob (summary/job.py)
# is the product-level fix -- it runs generation off the request/response cycle
# entirely, the same durable-job pattern the offline caption worker uses, so an
# operator is never blocked on a live HTTP request for minutes either way.
DEFAULT_GENERATE_TIMEOUT_SECONDS = 600
# Local generate requests share one resident Ollama process. Serialize CivicCast's
# callers so a summary and translation cannot load competing models at once.
_GENERATE_LOCK = threading.Lock()
# The short budget for cheap, fast metadata calls (/api/tags, /api/version,
# /api/ps) that must fail fast when Ollama is genuinely down rather than hang.
DEFAULT_TIMEOUT_SECONDS = 120


class OllamaRuntimeUnavailableError(RuntimeError):
    """Raised when a required local Ollama model or daemon is unavailable."""


@dataclass(frozen=True)
class OllamaModelManifest:
    """Live model metadata captured from a running Ollama daemon."""

    model: str
    digest: str
    runtime_version: str
    manifest_source: str
    details: dict[str, Any]


def get_ollama_model_manifest(
    model: str,
    *,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
) -> OllamaModelManifest:
    """Return the live digest and version for an installed Ollama model."""

    tags = _request_json("GET", f"{base_url.rstrip('/')}/api/tags")
    model_rows = tags.get("models", [])
    if not isinstance(model_rows, list):
        raise OllamaRuntimeUnavailableError("Ollama /api/tags returned an invalid model list.")

    row = next((item for item in model_rows if item.get("name") == model), None)
    if row is None:
        raise OllamaRuntimeUnavailableError(
            f"Ollama model {model!r} is not installed. Run `ollama pull {model}` and retry."
        )

    digest = row.get("digest")
    if not isinstance(digest, str) or len(digest) < 32:
        raise OllamaRuntimeUnavailableError(
            f"Ollama model {model!r} did not report a usable digest from /api/tags."
        )

    version_payload = _request_json("GET", f"{base_url.rstrip('/')}/api/version")
    version = version_payload.get("version")
    if not isinstance(version, str) or not version:
        raise OllamaRuntimeUnavailableError("Ollama /api/version did not report a version.")

    details = row.get("details")
    return OllamaModelManifest(
        model=model,
        digest="sha256:" + digest.removeprefix("sha256:"),
        runtime_version=f"ollama {version}",
        manifest_source=f"{base_url.rstrip('/')}/api/tags#{model}",
        details=details if isinstance(details, dict) else {},
    )


def list_local_model_names(*, base_url: str = DEFAULT_OLLAMA_BASE_URL) -> set[str] | None:
    """Installed local Ollama model names, or ``None`` when Ollama is unreachable.

    A non-raising probe for the S13 availability surface (Q2/U4): callers use ``None``
    to mean "AI runtime unavailable" and a returned set to test model presence. Never
    raises on a down daemon — that is the signal, not an error.
    """

    try:
        tags = _request_json("GET", f"{base_url.rstrip('/')}/api/tags")
    except OllamaRuntimeUnavailableError:
        return None
    model_rows = tags.get("models", [])
    if not isinstance(model_rows, list):
        return set()
    return {name for item in model_rows if isinstance(name := item.get("name"), str)}


def generate_with_ollama(
    *,
    model: str,
    prompt: str,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    timeout: float = DEFAULT_GENERATE_TIMEOUT_SECONDS,
) -> str:
    """Generate a non-streaming completion from local Ollama.

    ``timeout`` defaults to :data:`DEFAULT_GENERATE_TIMEOUT_SECONDS` (600s), not
    the short metadata-call budget -- see that constant's docstring for the field
    evidence a 120s socket timeout here used to discard a completion Ollama had
    already finished computing.
    """

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": 0,
        "options": {"temperature": 0},
    }
    with _GENERATE_LOCK:
        response = _request_json(
            "POST", f"{base_url.rstrip('/')}/api/generate", payload, timeout=timeout
        )
    text = response.get("response")
    if not isinstance(text, str):
        raise OllamaRuntimeUnavailableError("Ollama /api/generate returned no response text.")
    return text


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    _require_local_http(url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - _require_local_http restricts URL to loopback HTTP(S).
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - request URL is loopback-only.  # nosec B310
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OllamaRuntimeUnavailableError(
            f"Local Ollama request failed for {url}. Start Ollama and retry."
        ) from exc
    if not isinstance(result, dict):
        raise OllamaRuntimeUnavailableError(f"Ollama returned non-object JSON for {url}.")
    return result


def _require_local_http(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise OllamaRuntimeUnavailableError(
            "Ollama release proof only permits loopback HTTP(S) endpoints."
        )
