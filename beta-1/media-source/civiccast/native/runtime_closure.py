# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Element policy and dependency-closure core for the native Windows runtime.

Implements the pure half of `spec-packaging-closure`: which GStreamer plugins
the shipped runtime is allowed to contain (D3, "no GPL in the shipped
runtime"), and the static PE-import walk that turns those plugins into the
complete set of files the installer must lay down (D2).

Everything here is deliberately free of filesystem and PE parsing. The build
script (`scripts/build_native_runtime_closure.py`) supplies `imports_of` and
`resolve` backed by `pefile` and a staged upstream tree; the tests supply
dictionaries. The graph logic is therefore provable without shipping fixture
binaries, and the same code runs in both places.

Why a checked-in factory->plugin table instead of probing the build machine:
Media Foundation and NVENC register their factories only when the host has
matching GPU hardware, so `Gst.ElementFactory.find` returns different answers
on different build boxes. Deriving the closure from a live probe would make
the shipped tree machine-dependent and break AC1 (two clean runs from the same
pinned inputs must produce identical SHA256SUMS). The table is derived from
measured evidence and guarded by tests; runtime factory availability is a
separate question answered by the doctor probe on the operator's machine.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

__all__ = [
    "ABSENCE_TOLERANT_FACTORIES",
    "AUTHORIZED_RUNTIME_DISTRIBUTIONS",
    "AUTHORIZED_STAGED_DISTRIBUTIONS",
    "CONDITIONAL_FACTORIES",
    "EXCLUDED_GPL_FACTORIES",
    "FACTORY_PLUGIN",
    "GPL_DISTRIBUTIONS",
    "HARDWARE_GATED_FACTORIES",
    "LINUX_ONLY_FACTORIES",
    "NON_FACTORY_PLUGINS",
    "REQUIRED_FACTORIES",
    "STAGED_OPTIONAL_FACTORIES",
    "GplPolicyError",
    "MissingPluginError",
    "RuntimeClosureError",
    "UnauthorizedDistributionError",
    "UnknownProvenanceError",
    "assert_authorized_distributions",
    "assert_no_gpl_distributions",
    "canonical_distribution_name",
    "classify_missing_factories",
    "resolve_pe_closure",
    "select_plugin_seeds",
]


# ---------------------------------------------------------------------------
# License policy
# ---------------------------------------------------------------------------

#: Upstream pip distributions that carry GPL-licensed plugins. The shipped
#: runtime never installs these (see `requirements-native-runtime.in`); this
#: set is the belt-and-braces check in case one is reintroduced by accident.
GPL_DISTRIBUTIONS = frozenset(
    {
        "gstreamer_plugins_gpl",
        "gstreamer_plugins_gpl_restricted",
    }
)

#: The EXACT distributions the pinned lock may stage. Deny-by-default.
#:
#: Round 1 reported that a GPL distribution could slip past on a name variant;
#: I made name matching canonical and left the design wrong, which round 2
#: demonstrated by walking `civiccast-unknown-runtime` straight through both
#: gates. Blocking the names you already know about is not a supply-chain
#: control -- a renamed, replaced or injected distribution is by definition not
#: on any denylist you thought to write down.
#:
#: `setuptools` is here because `gstreamer-libs` pulls it in transitively. It
#: is authorised to be STAGED and is deliberately absent from the runtime set
#: below: "allowed to be present" and "allowed into the product" are different
#: questions and get different answers.
AUTHORIZED_STAGED_DISTRIBUTIONS = frozenset(
    {
        "gstreamer_libs",
        "gstreamer_plugins",
        "gstreamer_plugins_libs",
        "gstreamer_plugins_restricted",
        "gstreamer_python",
        "gstreamer_ext_runtime",
        "gstreamer_cli",
        "setuptools",
    }
)

#: The EXACT distributions permitted to contribute a file to the shipped tree.
AUTHORIZED_RUNTIME_DISTRIBUTIONS = frozenset(
    {
        "gstreamer_libs",
        "gstreamer_plugins",
        "gstreamer_plugins_libs",
        "gstreamer_plugins_restricted",
        "gstreamer_python",
        "gstreamer_ext_runtime",
        "gstreamer_cli",
    }
)

#: The REVIEWED OS dependency inventory -- the exact Windows-provided DLL
#: basenames this product is allowed to rely on without shipping them.
#:
#: Lives here rather than in the build script because two different consumers
#: need the same answer: the closure builder (deciding what need not be shipped)
#: and the D6 verifier (deciding whether a traced load from %SystemRoot% is a
#: reviewed dependency or an unexamined one). Round 2 found the verifier
#: permitting ALL of %SystemRoot%, which is not an inventory -- it is a
#: directory. Every name here was checked to exist on a real Windows before
#: being added; see OS_DEPENDENCY_FLOOR in the build script for what relying on
#: them implies.
OS_DEPENDENCY_INVENTORY = frozenset(
    {
        "kernel32.dll",
        "user32.dll",
        "advapi32.dll",
        "ole32.dll",
        "oleaut32.dll",
        "shell32.dll",
        "ws2_32.dll",
        "msvcrt.dll",
        "ucrtbase.dll",
        "bcrypt.dll",
        "crypt32.dll",
        "secur32.dll",
        "winmm.dll",
        "gdi32.dll",
        "dxgi.dll",
        "d3d11.dll",
        "d3d12.dll",
        "mfplat.dll",
        "mfreadwrite.dll",
        "mf.dll",
        "dwmapi.dll",
        "setupapi.dll",
        "cfgmgr32.dll",
        "iphlpapi.dll",
        "psapi.dll",
        "shlwapi.dll",
        "version.dll",
        "userenv.dll",
        "dbghelp.dll",
        "ntdll.dll",
        "rpcrt4.dll",
        "wldap32.dll",
        "normaliz.dll",
        "powrprof.dll",
        "msimg32.dll",
        "dnsapi.dll",
        "opengl32.dll",
        "dwrite.dll",
        "d2d1.dll",
        "bcryptprimitives.dll",
    }
)

_NAME_SEPARATOR_RUN = re.compile(r"[-_.]+")


def canonical_distribution_name(name: str) -> str:
    """PEP 503 canonical form: lowercase, runs of ``-``/``_``/``.`` collapsed to ``-``.

    Python packaging treats `GStreamer-Plugins-GPL`, `gstreamer_plugins_gpl` and
    `gstreamer.plugins.gpl` as the SAME project, and which spelling appears in
    metadata depends on which tool wrote it. Comparing raw strings therefore let
    an equivalent spelling walk straight past the GPL gates -- a gate whose
    entire purpose is "no GPL ever ships" must not be defeated by
    capitalisation. (Codex r1 finding CC-WS5-PKG-002, Critical.)
    """
    return _NAME_SEPARATOR_RUN.sub("-", name.strip()).lower()


#: The GPL distributions in canonical form, computed once so both gates compare
#: like with like rather than each re-deriving it.
_CANONICAL_GPL_DISTRIBUTIONS = frozenset(
    canonical_distribution_name(name) for name in GPL_DISTRIBUTIONS
)

#: Encoders that must never ship, by name, independently of which distribution
#: happens to carry them. ADR-0021: "No GPL in the shipped runtime; x264
#: no-ship stands; openh264enc default."
EXCLUDED_GPL_FACTORIES = frozenset({"x264enc", "x265enc"})


# ---------------------------------------------------------------------------
# Element policy
# ---------------------------------------------------------------------------

#: Factory -> the GStreamer plugin module that provides it.
#:
#: Measured against the pinned upstream inputs (gstreamer-* 1.28.5 MSVC wheels)
#: rather than assumed. `tests/native/test_runtime_closure.py` guards the
#: table's completeness; the build script fails loudly if a named plugin is
#: absent from the staged tree.
FACTORY_PLUGIN: Mapping[str, str] = {
    # -- core elements (gstreamer_libs) ------------------------------------
    "appsink": "gstapp.dll",
    "appsrc": "gstapp.dll",
    "audiochebband": "gstaudiofx.dll",
    "audioconvert": "gstaudioconvert.dll",
    "audioloudnorm": "gstrsaudiofx.dll",
    "volume": "gstvolume.dll",
    "audioresample": "gstaudioresample.dll",
    "audiotestsrc": "gstaudiotestsrc.dll",
    "capsfilter": "gstcoreelements.dll",
    "concat": "gstcoreelements.dll",
    "filesink": "gstcoreelements.dll",
    "filesrc": "gstcoreelements.dll",
    "input-selector": "gstcoreelements.dll",
    "queue": "gstcoreelements.dll",
    "tee": "gstcoreelements.dll",
    "videoconvert": "gstvideoconvertscale.dll",
    "videorate": "gstvideorate.dll",
    "videoscale": "gstvideoconvertscale.dll",
    "videotestsrc": "gstvideotestsrc.dll",
    # -- parsers / containers ----------------------------------------------
    "aacparse": "gstaudioparsers.dll",
    "h264parse": "gstvideoparsersbad.dll",
    "h265parse": "gstvideoparsersbad.dll",
    "matroskademux": "gstmatroska.dll",
    "qtdemux": "gstisomp4.dll",
    "tsdemux": "gstmpegtsdemux.dll",
    "tsparse": "gstmpegtsdemux.dll",
    "mpegtsmux": "gstmpegtsmux.dll",
    "flvmux": "gstflv.dll",
    # -- decode / playback --------------------------------------------------
    "decodebin": "gstplayback.dll",
    "decodebin3": "gstplayback.dll",
    "uridecodebin": "gstplayback.dll",
    "avdec_aac": "gstlibav.dll",
    "avdec_h264": "gstlibav.dll",
    "avenc_aac": "gstlibav.dll",
    "openh264dec": "gstopenh264.dll",
    "d3d12h264dec": "gstd3d12.dll",
    # -- captions -----------------------------------------------------------
    "cccombiner": "gstclosedcaption.dll",
    "ccconverter": "gstclosedcaption.dll",
    "h264ccinserter": "gstclosedcaption.dll",
    "tttocea608": "gstrsclosedcaption.dll",
    "subparse": "gstsubparse.dll",
    # -- encoders -----------------------------------------------------------
    "openh264enc": "gstopenh264.dll",
    "voaacenc": "gstvoaacenc.dll",
    "mfh264enc": "gstmediafoundation.dll",
    "mfh265enc": "gstmediafoundation.dll",
    "nvh264enc": "gstnvcodec.dll",
    "nvh265enc": "gstnvcodec.dll",
    # -- transport ----------------------------------------------------------
    "rtmpsrc": "gstrtmp.dll",
    "rtmp2sink": "gstrtmp2.dll",
    "rtspsrc": "gstrtsp.dll",
    "srtsink": "gstsrt.dll",
    "srtsrc": "gstsrt.dll",
    "udpsink": "gstudp.dll",
    "udpsrc": "gstudp.dll",
    "souphttpsrc": "gstsoup.dll",
    # -- SDI (roadmap hardware; plugin ships, device optional) --------------
    "decklinkvideosink": "gstdecklink.dll",
    "decklinkvideosrc": "gstdecklink.dll",
    # -- absence-tolerant decoders (engine.py rank demotion) ----------------
    "nvh264dec": "gstnvcodec.dll",
    "nvh265dec": "gstnvcodec.dll",
    "nvav1dec": "gstnvcodec.dll",
    "cudah264dec": "gstnvcodec.dll",
    "cudah265dec": "gstnvcodec.dll",
    "d3d11h264dec": "gstd3d11.dll",
    "d3d11h265dec": "gstd3d11.dll",
    # -- excluded (mapped so the negative control has something to refuse) --
    "x264enc": "gstx264.dll",
    "x265enc": "gstx265.dll",
    # -- S15 CG-lite / native-HLS (staged, not yet engine-required) ---------
    "compositor": "gstcompositor.dll",
    "textoverlay": "gstpango.dll",
    "clockoverlay": "gstpango.dll",
    "timeoverlay": "gstpango.dll",
    "hlssink3": "gsthlssink3.dll",
}

#: The 52 factories the product's pipelines cannot run without. Derived from
#: the spike's measured closure
#: (`.agent-runs/native-windows/spike-gstreamer-bundle/evidence/elements-required.txt`)
#: with deliberate corrections/additions: GPL `x264enc` is dropped (no-ship),
#: `mfh265enc` is added for native HEVC, `appsink` for live captions, and the
#: audioloudnorm/audiochebband pair for per-output normalization and EAS
#: filtering. Their DLLs come from the already-pinned gstreamer-plugins wheel.
#: The count is asserted so runtime graph growth cannot silently outrun closure.
REQUIRED_FACTORIES = frozenset(
    {
        "aacparse",
        "appsink",
        "appsrc",
        "audioconvert",
        "audiochebband",
        "audioloudnorm",
        "volume",
        "audioresample",
        "audiotestsrc",
        "avdec_aac",
        "avdec_h264",
        "avenc_aac",
        "capsfilter",
        "cccombiner",
        "ccconverter",
        "concat",
        "d3d12h264dec",
        "decklinkvideosink",
        "decklinkvideosrc",
        "decodebin",
        "decodebin3",
        "filesink",
        "filesrc",
        "flvmux",
        "h264ccinserter",
        "h264parse",
        "h265parse",
        "input-selector",
        "matroskademux",
        "mfh264enc",
        "mfh265enc",
        "mpegtsmux",
        "openh264dec",
        "openh264enc",
        "qtdemux",
        "queue",
        "rtmp2sink",
        "rtmpsrc",
        "rtspsrc",
        "souphttpsrc",
        "srtsink",
        "srtsrc",
        "subparse",
        "tee",
        "tsdemux",
        "tsparse",
        "tttocea608",
        "udpsink",
        "udpsrc",
        "uridecodebin",
        "videoconvert",
        "videorate",
        "videoscale",
        "videotestsrc",
        "voaacenc",
    }
)

#: S15 CG-lite / native-HLS factories staged into the shipped tree AHEAD of
#: any pipeline using them, per PR #88's disposition: `_BASE_REQUIRED_PLUGINS`
#: (the commissioning probe's gate, `civiccast.platform.station_box_profile`)
#: was found to demand `compositor`/`textoverlay`/`clockoverlay`/`hlssink3`
#: that the shipped engine genuinely never used, and no pipeline in
#: `civiccast/egress/gst/engine.py` builds a graph with any of these
#: factories today -- so they do NOT belong in `REQUIRED_FACTORIES`, whose
#: docstring is specifically "the factories the product's pipelines cannot
#: run without". They belong here instead: plugins whose DLLs already ship
#: in the pinned `gstreamer-libs`/`gstreamer-plugins` 1.28.5 wheels (no new
#: upstream artifact, no version bump) and are staged now so the S15
#: CG-lite compositing and native-HLS work can start against a runtime that
#: already carries them, rather than needing a separate packaging change
#: later.
#:
#: Unlike `CONDITIONAL_FACTORIES`/`ABSENCE_TOLERANT_FACTORIES`, presence
#: here is NOT gated on "is it in `origins`" -- these are unconditionally
#: folded into the build script's `required` set, so a build refuses loudly
#: (`MissingPluginError`) if a future upstream bump ever drops one, instead
#: of silently shipping a tree missing the plugin. `interpipesrc`/
#: `interpipesink` are deliberately NOT here: PR #88 recorded that the
#: RidgeRun interpipe plugin is not in the pinned wheels at all and would
#: need a new upstream artifact, which is out of scope for an additive,
#: no-new-download change.
STAGED_OPTIONAL_FACTORIES = frozenset({"compositor", "textoverlay", "clockoverlay", "hlssink3"})

#: Profile-selected encoders that ship when their plugin is present but whose
#: *factories* only register on matching hardware. NVENC is the NVIDIA path;
#: the Media Foundation pair is in REQUIRED because it is the vendor-agnostic
#: fallback the target AMD-iGPU station depends on.
CONDITIONAL_FACTORIES = frozenset({"nvh264enc", "nvh265enc"})

#: Decoders `engine.py` demotes by rank when present. Their absence is normal
#: and never an error -- the pipeline falls through to a software decoder.
ABSENCE_TOLERANT_FACTORIES = frozenset(
    {
        "cudah264dec",
        "cudah265dec",
        "d3d11h264dec",
        "d3d11h265dec",
        "nvav1dec",
        "nvh264dec",
        "nvh265dec",
    }
)

#: REQUIRED factories whose plugin DLL always ships but whose registration
#: depends on the host's hardware: the Media Foundation encoders need a
#: matching MFT, NVENC needs an NVIDIA GPU, and `d3d12h264dec` needs a D3D12
#: adapter with video-decode support. Promoted here from
#: `scripts/verify_native_runtime_closure.py` (2026-08-07) when candidate run
#: 31190955761 -- the first ever to get the installed-product smoke past
#: `import gi` -- failed on `d3d12h264dec`/`mfh265enc` "missing" on a
#: GPU-less hosted runner: the closure verifier already excused
#: hardware-gated absences, but `installed_gstreamer_smoke` hard-required
#: every REQUIRED factory because this knowledge lived only in the build
#: script, which does not ship. The classifier below is the single shared
#: rule; the packaging guarantee is unchanged (a gated name is excusable
#: only when its plugin FILE is present -- callers answer that filesystem
#: question themselves, this module stays filesystem-free).
HARDWARE_GATED_FACTORIES = frozenset(
    {"mfh264enc", "mfh265enc", "nvh264enc", "nvh265enc", "d3d12h264dec"}
)

#: Plugins the shipped tree requires that NO element factory names, so the
#: factory-driven seed selection (`select_plugin_seeds` over `FACTORY_PLUGIN`)
#: can never pull them in. GStreamer's `typefind` ELEMENT ships via
#: `gstcoreelements.dll`, but the format DETECTORS it delegates to are
#: typefinder features registered by `gsttypefindfunctions.dll` -- a plugin
#: that exports no element factory at all. Without it the installed tree
#: cannot identify ANY media file: `uridecodebin`/`decodebin` (both REQUIRED
#: above) and the shipped `gst-discoverer` (the closure's own "independently
#: executable validation target") all fail with "Could not determine type of
#: stream" no matter how valid the input is.
#:
#: Found by candidate run 31205696163, the first to run the installed smoke's
#: discoverer leg: the worker's MPEG-TS was byte-valid (sync pattern verified
#: by the smoke's forensics) yet undiscoverable in the installed tree, while
#: the identical file discovered cleanly against the full wheel set. Proven
#: both directions locally on one machine and one file: with this plugin the
#: discoverer identifies the stream; with only this plugin removed it emits
#: the exact CI failure text. Station impact is real, not cosmetic -- every
#: auto-plug ingest path (operator VOD file import via decodebin) depends on
#: these detectors.
NON_FACTORY_PLUGINS = frozenset({"gsttypefindfunctions.dll"})


def classify_missing_factories(
    missing: Iterable[str],
    *,
    hardware_gated: frozenset[str] = HARDWARE_GATED_FACTORIES,
    plugin_file_missing: frozenset[str] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a list of missing factory names into
    ``(hardware_gated, genuine)``, both sorted.

    Two independent questions decide the split, per the adversarial-review
    finding that a required factory's plugin DLL being entirely absent from
    the tree was previously masked as an excused hardware gate:

    - Is the factory's PLUGIN FILE present in the tree? A packaging
      question. ``plugin_file_missing`` names the factories for which the
      answer is "no" -- that is ALWAYS a genuine miss, regardless of
      whether the name is also in ``hardware_gated``.
    - Does the FACTORY REGISTER on this machine? A hardware question. Only
      a name in ``hardware_gated`` whose plugin file is genuinely present
      (i.e. NOT in ``plugin_file_missing``) may be excused.

    ``plugin_file_missing`` defaults to empty, i.e. "no known-absent plugin
    files" -- callers that don't care about file presence (most existing
    tests) get the pre-existing hardware-gated/genuine split unchanged.
    """

    missing_set = set(missing)
    excusable = missing_set & hardware_gated
    gated = tuple(sorted(excusable - plugin_file_missing))
    genuine = tuple(sorted(missing_set - set(gated)))
    return gated, genuine


#: VAAPI factory names that exist only on Linux. They appear in the WSL-era
#: element lists and in `bridge.py`'s *config* vocabulary, where the
#: encoder-remap slice translates them to Media Foundation on Windows. They are
#: never GStreamer factories in a Windows runtime, so they are deliberately
#: absent from FACTORY_PLUGIN rather than mapped to a plugin that cannot exist.
LINUX_ONLY_FACTORIES = frozenset(
    {
        "vaapih264dec",
        "vaapih265dec",
        "vah264dec",
        "vah264enc",
        "vah265dec",
        "vah265enc",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RuntimeClosureError(RuntimeError):
    """Base class for packaging-closure build refusals."""


class GplPolicyError(RuntimeClosureError):
    """A selected plugin would put GPL-licensed bytes in the shipped runtime."""


class UnauthorizedDistributionError(RuntimeClosureError):
    """A distribution outside the exact authorised set appeared.

    Distinct from `GplPolicyError` on purpose: that one means "we recognised
    this and it is forbidden", this one means "we do not recognise this at all",
    and the second is the more dangerous of the two.
    """


class MissingPluginError(RuntimeClosureError):
    """A required factory has no plugin in the staged upstream tree."""


class UnknownProvenanceError(RuntimeClosureError):
    """A PE import resolved neither inside the tree nor to an allowed OS DLL."""


# ---------------------------------------------------------------------------
# Seed selection
# ---------------------------------------------------------------------------


def assert_no_gpl_distributions(distributions: Iterable[str]) -> None:
    """Refuse if any GPL-licensed distribution was staged at all.

    This guards the INPUT boundary, which `select_plugin_seeds` does not:
    selection-time refusal only fires when a GPL factory is actually chosen, so
    a lockfile that quietly re-added `gstreamer-plugins-gpl` would stage it,
    ship nothing from it, and produce no signal that the no-GPL input contract
    had been broken. The lockfile is the artifact under review; a silent pass
    there is how the NEXT change ships x264enc for real.
    """

    offenders = sorted(
        name
        for name in set(distributions)
        if canonical_distribution_name(name) in _CANONICAL_GPL_DISTRIBUTIONS
    )
    if offenders:
        raise GplPolicyError(
            "Refusing to build the native runtime closure -- GPL-licensed "
            "distributions are present in the staged inputs: "
            + ", ".join(offenders)
            + ". ADR-0021 forbids GPL in the shipped runtime; remove them from "
            "requirements-native-runtime.in rather than relying on them not "
            "being selected."
        )


def assert_authorized_distributions(distributions: Iterable[str], *, runtime: bool = False) -> None:
    """Refuse anything outside the exact authorised set (deny-by-default).

    ``runtime=False`` checks what may be STAGED; ``runtime=True`` checks what
    may contribute a shipped file, which is the stricter set.

    This runs BEFORE and INDEPENDENTLY of the GPL check. The GPL gate answers
    "is this a thing we know to be forbidden"; this one answers "is this a thing
    we authorised at all". An unrecognised distribution is the more dangerous
    case, because nothing downstream has any basis for reasoning about it -- the
    per-file licence classifier, for instance, would fall back to reasoning from
    a familiar-looking basename.
    """

    authorized = AUTHORIZED_RUNTIME_DISTRIBUTIONS if runtime else AUTHORIZED_STAGED_DISTRIBUTIONS
    canonical_authorized = {canonical_distribution_name(name): name for name in authorized}

    offenders = sorted(
        {
            name
            for name in distributions
            if canonical_distribution_name(name) not in canonical_authorized
        }
    )
    if offenders:
        scope = "contribute files to the shipped runtime" if runtime else "be staged"
        raise UnauthorizedDistributionError(
            "Refusing to build the native runtime closure -- distribution(s) not "
            f"authorised to {scope}: "
            + ", ".join(offenders)
            + ". Authorised set: "
            + ", ".join(sorted(authorized))
            + ". Add it to the authorised set in civiccast.native.runtime_closure only "
            "after establishing its provenance and licence -- an unrecognised "
            "distribution is never assumed safe."
        )


def select_plugin_seeds(
    origins: Mapping[str, tuple[str, str]],
    *,
    required: Iterable[str],
) -> tuple[str, ...]:
    """Return the plugin files that satisfy ``required``, or refuse.

    ``origins`` maps a factory name to ``(plugin path, owning distribution)``,
    where the path is relative to the staged upstream root.

    Refuses -- never warns, never drops silently -- when a selected factory is
    GPL-excluded or comes from a GPL distribution, and when a required factory
    has no plugin at all. A silently dropped plugin is a channel that fails to
    start on the operator's machine hours after the installer said "success".
    """

    wanted = sorted(set(required))

    violations: list[str] = []
    for factory in wanted:
        origin = origins.get(factory)
        distribution = origin[1] if origin is not None else "<not staged>"
        if factory in EXCLUDED_GPL_FACTORIES:
            violations.append(
                f"{factory} (from {distribution}) is on the no-ship list: "
                "ADR-0021 forbids GPL encoders in the shipped runtime"
            )
        elif (
            origin is not None
            and canonical_distribution_name(distribution) in _CANONICAL_GPL_DISTRIBUTIONS
        ):
            violations.append(
                f"{factory} comes from the GPL distribution {distribution}, "
                "which the native runtime never installs"
            )
    if violations:
        raise GplPolicyError(
            "Refusing to build the native runtime closure -- GPL policy violated:\n  "
            + "\n  ".join(violations)
        )

    # Deny-by-default, AFTER the GPL check. Both refuse, so the order does not
    # change what is accepted -- only which message the operator gets. A
    # RECOGNISED GPL distribution deserves the specific, actionable GPL error
    # ("this is forbidden, and here is the policy"), not a generic "not on the
    # list". Anything we do not recognise falls through to here.
    assert_authorized_distributions(
        {origins[factory][1] for factory in wanted if factory in origins}, runtime=True
    )

    missing = [factory for factory in wanted if factory not in origins]
    if missing:
        raise MissingPluginError(
            "Required GStreamer factories have no plugin in the staged upstream tree: "
            + ", ".join(missing)
        )

    return tuple(sorted({origins[factory][0] for factory in wanted}))


# ---------------------------------------------------------------------------
# PE import closure
# ---------------------------------------------------------------------------


def resolve_pe_closure(
    seeds: Iterable[str],
    *,
    imports_of: Callable[[str], Iterable[str]],
    resolve: Callable[[str], str | None],
    system_allowlist: Iterable[str],
) -> tuple[str, ...]:
    """Walk PE imports from ``seeds`` and return every in-tree file reached.

    ``imports_of`` yields the DLL names a tree file imports; ``resolve`` maps an
    imported DLL name to its path inside the tree, or ``None`` when it is not
    there. Names in ``system_allowlist`` are Windows-provided and expected to
    resolve outside the tree.

    Import tables are matched case-insensitively because the casing a linker
    emits is not stable across upstream builds. Cycles are tolerated. Anything
    that resolves neither in-tree nor to an allowed OS DLL raises
    `UnknownProvenanceError` reporting *every* such import together with the
    file that imported it -- one round trip per build, not one per missing DLL.
    """

    allowed = {name.lower() for name in system_allowlist}
    closure: set[str] = set()
    unresolved: list[tuple[str, str]] = []

    pending = list(seeds)
    while pending:
        path = pending.pop()
        if path in closure:
            continue
        closure.add(path)
        for imported in imports_of(path):
            if imported.lower() in allowed:
                continue
            target = resolve(imported)
            if target is None:
                unresolved.append((path, imported))
                continue
            if target not in closure:
                pending.append(target)

    if unresolved:
        detail = "\n  ".join(
            f"{imported} imported by {importer}" for importer, imported in sorted(unresolved)
        )
        raise UnknownProvenanceError(
            "Refusing to build the native runtime closure -- imports resolve neither "
            "inside the packaged tree nor to an allowed Windows system DLL. Ship them "
            "or add them to the system allowlist; never leave them dangling:\n  " + detail
        )

    return tuple(sorted(closure))
