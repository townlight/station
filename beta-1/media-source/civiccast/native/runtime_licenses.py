# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Per-FILE license provenance for the native Windows runtime closure.

`spec-packaging-closure` D3: "`gst-inspect` license metadata is an INPUT,
not the authority -- each plugin's license is confirmed against upstream
source license files for the exact version." This module is the result of
that confirmation, not a restatement of the wheels' own aggregate `License`
classifier (which covers the WHOLE wheel; we ship a pruned subset of each
one, so the aggregate is an upper bound, never our bill of materials).

Full investigation, exact commands, and verbatim output live in
`.agent-runs/native-windows/ws5-packaging-closure/evidence/
license-provenance.md`. Read that file for the evidence behind every entry
below; this module only encodes the conclusions.

Deliberately independent of `runtime_manifest.py` (another worker owns that
file concurrently as of this writing) -- nothing here imports from it, and
nothing in `runtime_manifest.py` imports from here yet. `classify_shipped_
file` is written to be a drop-in per-path license resolver for whoever wires
this in next.

AC7 is the design constraint throughout: `classify_shipped_file` returns
`None` for anything this investigation did not confirm -- never a guessed
default, never falling back to the loosest or the strictest license in the
table. A `None` result is a provenance gap, not a "probably fine".
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "CUDA_PACK_BIN_BASENAME_PREFIX_LICENSE",
    "CUDA_PACK_PATH_PREFIX_LICENSE",
    "CUDA_TOOLKIT_EULA_LICENSE",
    "CUDNN_EULA_LICENSE",
    "DUAL_LICENSE_ELECTIONS",
    "FFMPEG_PACK_BASENAME_LICENSE",
    "FFMPEG_PACK_PATH_PREFIX_LICENSE",
    "FONTCONFIG_2_16_1_LICENSE",
    "GENERATED_ARTIFACT_LICENSE",
    "GENERATED_ARTIFACT_PATHS",
    "GPL_EXCLUDED_PLUGIN_BASENAMES",
    "PATH_PREFIX_LICENSE",
    "PLUGIN_LICENSE",
    "SERVER_PACK_BASENAME_LICENSE",
    "SERVER_PACK_PATH_PREFIX_LICENSE",
    "SUPPORT_LIBRARY_LICENSE",
    "TYPELIB_LICENSE",
    "UNRESOLVED_BASENAMES",
    "classify_cuda_pack_file",
    "classify_ffmpeg_pack_file",
    "classify_server_pack_file",
    "classify_shipped_file",
    "is_gpl_license",
    "license_identifiers_in",
]


# ---------------------------------------------------------------------------
# Category 1 -- GStreamer plugin DLLs (lib/gstreamer-1.0/*.dll)
#
# Evidence: `gst_plugin_get_license()` queried directly against every plugin
# DLL loaded from the actual built tree (34 plugins; see evidence memo
# "Plugin sweep against the built tree"). GStreamer's own coarse license
# vocabulary ("LGPL", "BSD", "MPL") is normalized here to the specific SPDX
# identifier that vocabulary maps to for each project (GStreamer core/base/
# good/ugly plugins are LGPL-2.1-or-later per COPYING.LIB; OpenH264 is
# BSD-2-Clause per Cisco's own LICENSE; the Rust closed-caption plugin is
# MPL-2.0 per the gst-plugins-rs workspace license).
# ---------------------------------------------------------------------------

PLUGIN_LICENSE: Final[dict[str, str]] = {
    "gstapp.dll": "LGPL-2.1-or-later",
    # Probed 2026-10-13 with gst-inspect-1.0 audiochebband in the pinned
    # 1.28.5 tree: License=LGPL, Source module=gst-plugins-good. The module's
    # upstream COPYING.LIB is LGPL-2.1-or-later.
    "gstaudiofx.dll": "LGPL-2.1-or-later",
    "gstvolume.dll": "LGPL-2.1-or-later",
    "gstaudioconvert.dll": "LGPL-2.1-or-later",
    "gstaudioparsers.dll": "LGPL-2.1-or-later",
    "gstaudioresample.dll": "LGPL-2.1-or-later",
    "gstaudiotestsrc.dll": "LGPL-2.1-or-later",
    "gstclosedcaption.dll": "LGPL-2.1-or-later",
    # gstcompositor.dll, gstpango.dll, gsthlssink3.dll: staged 2026-08-30 as
    # STAGED_OPTIONAL_FACTORIES (S15 CG-lite / native-HLS, PR #88's
    # disposition) -- the first build of this closure to ship them, so
    # unlike the entries above there is no live gst_plugin_get_license()
    # probe against a BUILT tree containing these three yet. Recorded from
    # documentation instead: gstcompositor/gstpango are gst-plugins-base C
    # plugins covered by the same upstream COPYING.LIB (LGPL-2.1-or-later)
    # as every other plugin in this table drawn from that module, and
    # `gstreamer-libs`/`gstreamer-plugins` 1.28.7's own METADATA declares
    # "LGPL" project-wide (see `licenses/gstreamer_libs.txt` /
    # `licenses/gstreamer_plugins.txt` after a build). A live probe against
    # the first built tree that actually contains them is a real follow-up,
    # same as `gsttypefindfunctions.dll` got above -- reviewed-against-
    # documentation is recorded as the weaker claim it is, not upgraded to
    # "observed" before it has been. That same documentation-derived premise
    # was WRONG for the third of the three; see the probe below.
    #
    # U17 live probe, 2026-09-24, `gst-inspect-1.0 <plugin.dll>` against the
    # BUILT 1.28.7 tree (and the installed 1.28.5 tree, for comparison):
    #   gstcompositor.dll  -> License=LGPL, Source module=gst-plugins-base
    #   gstpango.dll       -> License=LGPL, Source module=gst-plugins-base
    #   gsthlssink3.dll    -> License=MPL,  Source module=gst-plugin-hlssink3
    # The first two agree with the entries below. The third did NOT: it is a
    # gst-plugins-rs Rust plugin (MPL-2.0 is that family's licence), not a
    # gst-plugins-base/gst-plugins-good C plugin. The probe is identical on
    # the installed 1.28.5 tree, i.e. PRE-EXISTING, not introduced by the
    # 1.28.5 -> 1.28.7 update.
    #
    # CORRECTED 2026-09-24 (U16, owner decision, Scott Converse): the entry
    # below now records MPL-2.0 -- the licence the live probe actually
    # observed -- instead of the documented LGPL-2.1-or-later. The value
    # feeds the shipped per-file licence table rendered by
    # `runtime_manifest.render_license_bom` into LICENSE-BOM.md; that file is
    # a BUILD artifact (not tracked in this repo), so the next closure build
    # renders the corrected value and any artifact built before this date
    # still carries the old one -- regenerate it, do not hand-edit it.
    "gstcompositor.dll": "LGPL-2.1-or-later",
    "gstpango.dll": "LGPL-2.1-or-later",
    "gsthlssink3.dll": "MPL-2.0",
    "gstcoreelements.dll": "LGPL-2.1-or-later",
    "gstd3d11.dll": "LGPL-2.1-or-later",
    "gstd3d12.dll": "LGPL-2.1-or-later",
    "gstdecklink.dll": "LGPL-2.1-or-later",
    "gstflv.dll": "LGPL-2.1-or-later",
    "gstisomp4.dll": "LGPL-2.1-or-later",
    # gstlibav.dll wraps the shipped FFmpeg build. Its OWN plugin-metadata
    # license (probed) is LGPL, corroborated independently by FFmpeg's own
    # avcodec_license()/avutil_license() (see SUPPORT_LIBRARY_LICENSE below)
    # returning "LGPL version 2.1 or later" with nonfree=disabled,
    # version3=disabled, and no libx264/libx265 registered as codecs.
    "gstlibav.dll": "LGPL-2.1-or-later",
    "gstmatroska.dll": "LGPL-2.1-or-later",
    "gstmediafoundation.dll": "LGPL-2.1-or-later",
    "gstmpegtsdemux.dll": "LGPL-2.1-or-later",
    "gstmpegtsmux.dll": "LGPL-2.1-or-later",
    "gstnvcodec.dll": "LGPL-2.1-or-later",
    "gstopenh264.dll": "BSD-2-Clause",
    "gstplayback.dll": "LGPL-2.1-or-later",
    # Probed 2026-10-13 with gst-inspect-1.0 audioloudnorm in the pinned
    # 1.28.5 tree: License=MPL, Source module=gst-plugin-audiofx, version
    # 0.15.3-cee45224c. This Rust plugin follows gst-plugins-rs' MPL-2.0
    # workspace license, matching gstrsclosedcaption.dll below.
    "gstrsaudiofx.dll": "MPL-2.0",
    "gstrsclosedcaption.dll": "MPL-2.0",
    "gstrtmp.dll": "LGPL-2.1-or-later",
    "gstrtmp2.dll": "LGPL-2.1-or-later",
    "gstrtsp.dll": "LGPL-2.1-or-later",
    "gstsoup.dll": "LGPL-2.1-or-later",
    "gstsrt.dll": "LGPL-2.1-or-later",
    "gstsubparse.dll": "LGPL-2.1-or-later",
    # Probed 2026-08-07 when NON_FACTORY_PLUGINS first shipped it (the
    # provenance gate on candidate run 31208490253 refused the file, as
    # designed): gst_plugin_get_license() via gst-inspect-1.0 against the
    # pinned 1.28.5 DLL reports License=LGPL, Source module
    # gst-plugins-base -- normalized per this table's rule to the SPDX id
    # below.
    "gsttypefindfunctions.dll": "LGPL-2.1-or-later",
    "gstudp.dll": "LGPL-2.1-or-later",
    "gstvideoconvertscale.dll": "LGPL-2.1-or-later",
    "gstvideoparsersbad.dll": "LGPL-2.1-or-later",
    "gstvideorate.dll": "LGPL-2.1-or-later",
    "gstvideotestsrc.dll": "LGPL-2.1-or-later",
    "gstvoaacenc.dll": "LGPL-2.1-or-later",
}

#: The six plugin DLLs that exist ONLY in the two wheels this build
#: deliberately never installs (`gstreamer_plugins_gpl`,
#: `gstreamer_plugins_gpl_restricted` -- confirmed absent from
#: `requirements-native-runtime.txt`). Each self-reports license "GPL" via
#: `gst_plugin_get_license()` when probed directly (evidence memo,
#: "Excluded GPL wheels" section). Recorded here so a future accidental
#: inclusion is instantly recognizable, NOT as a license this module
#: resolves -- `classify_shipped_file` deliberately has no entry for these
#: basenames and returns `None` for them, same as any other file this
#: investigation never examined.
GPL_EXCLUDED_PLUGIN_BASENAMES: Final[frozenset[str]] = frozenset(
    {
        "gstx264.dll",
        "gstx265.dll",
        "gsta52dec.dll",
        "gstdtsdec.dll",
        "gstdvdread.dll",
        "gstresindvd.dll",
    }
)


# ---------------------------------------------------------------------------
# Category 2 -- support libraries (bin/*.dll) and gio modules
# (lib/gio/modules/*.dll)
#
# Evidence varies by entry; see the inline comment on each group. Where a
# project is genuinely dual-licensed with a GPL branch (librtmp, cairo/
# cairo-gobject, FreeType), the value recorded here is the ELECTED non-GPL
# branch, not a guess -- the dual license is a choice available to the
# redistributor, and choosing the non-GPL branch is a valid, honest
# election, not an assertion that the GPL branch does not exist. Every
# basename with a genuine dual-license election is also listed in
# DUAL_LICENSE_ELECTIONS below, so the election is a checkable fact, not
# just prose in a comment a future reader could miss.
# ---------------------------------------------------------------------------

#: fontconfig 2.16.1's full, cumulative SPDX expression -- see the
#: `fontconfig-1.dll` entry below for the per-component derivation and the
#: upstream commit it was read from. Named once and referenced by both the
#: DLL and its typelib so the two can never drift apart into disagreeing
#: about the same upstream component.
FONTCONFIG_2_16_1_LICENSE: Final[str] = (
    "HPND-sell-variant AND MIT AND MIT-Modern-Variant AND Unicode-TOU "
    "AND LicenseRef-Fontconfig-2.16.1"
)

SUPPORT_LIBRARY_LICENSE: Final[dict[str, str]] = {
    # --- GStreamer CLI consumers: gstreamer-cli 1.28.5, LGPL-2.1-or-later.
    # These are staged deliberately as real installed-runtime consumers; their
    # import closure is walked exactly like plugin/PyGObject seed binaries.
    "gst-discoverer-1.0.exe": "LGPL-2.1-or-later",
    "gst-inspect-1.0.exe": "LGPL-2.1-or-later",
    # --- FFmpeg: direct runtime probe (avutil_license(), avcodec_license(),
    # avformat_license(), avfilter_license(), swscale_license(),
    # swresample_license() all called via ctypes against the shipped DLLs;
    # every one returned "LGPL version 2.1 or later"). Build configuration
    # string (avutil_configuration()) shows nonfree=disabled,
    # version3=disabled; avcodec_find_encoder_by_name() confirms no
    # libx264/libx265/libopenh264/libfdk_aac registered among the build's
    # 673 codecs. See evidence memo "FFmpeg" section for verbatim output.
    "avutil-59.dll": "LGPL-2.1-or-later",
    "avcodec-61.dll": "LGPL-2.1-or-later",
    "avformat-61.dll": "LGPL-2.1-or-later",
    "avfilter-10.dll": "LGPL-2.1-or-later",
    "swresample-5.dll": "LGPL-2.1-or-later",
    "swscale-8.dll": "LGPL-2.1-or-later",
    # --- Cisco OpenH264 codec library itself (distinct file from the
    # gstopenh264.dll GStreamer plugin wrapper above). BSD-2-Clause per
    # Cisco's own upstream LICENSE, corroborated by the wrapping plugin's
    # own "BSD" self-report. The SEPARATE question of Cisco's binary
    # patent-non-assertion posture (spec D3's explicit owner-acceptance
    # item) is a patent question, not an SPDX license question -- flagged
    # in the evidence memo, not resolved by this classification.
    "openh264-7.dll": "BSD-2-Clause",
    # --- VisualOn AAC encoder: Apache-2.0 per upstream LICENSE, matches
    # "Apache-2.0" in gstreamer_plugins_restricted's own METADATA aggregate.
    "vo-aacenc-0.dll": "Apache-2.0",
    # --- GStreamer/GLib core runtime libraries: LGPL-2.1-or-later per each
    # project's own COPYING.LIB, cross-referenced against the coreelements
    # plugin's own "LGPL" self-report (these libraries ARE what implements
    # gst_plugin_get_license()).
    "gio-2.0-0.dll": "LGPL-2.1-or-later",
    "girepository-1.0-1.dll": "LGPL-2.1-or-later",
    "glib-2.0-0.dll": "LGPL-2.1-or-later",
    "gmodule-2.0-0.dll": "LGPL-2.1-or-later",
    "gobject-2.0-0.dll": "LGPL-2.1-or-later",
    "gstanalytics-1.0-0.dll": "LGPL-2.1-or-later",
    "gstapp-1.0-0.dll": "LGPL-2.1-or-later",
    "gstaudio-1.0-0.dll": "LGPL-2.1-or-later",
    "gstbase-1.0-0.dll": "LGPL-2.1-or-later",
    "gstcodecparsers-1.0-0.dll": "LGPL-2.1-or-later",
    "gstcodecs-1.0-0.dll": "LGPL-2.1-or-later",
    "gstcuda-1.0-0.dll": "LGPL-2.1-or-later",
    "gstd3d11-1.0-0.dll": "LGPL-2.1-or-later",
    "gstd3d12-1.0-0.dll": "LGPL-2.1-or-later",
    "gstd3dshader-1.0-0.dll": "LGPL-2.1-or-later",
    "gstdxva-1.0-0.dll": "LGPL-2.1-or-later",
    "gstgl-1.0-0.dll": "LGPL-2.1-or-later",
    "gstmpegts-1.0-0.dll": "LGPL-2.1-or-later",
    "gstnet-1.0-0.dll": "LGPL-2.1-or-later",
    "gstpbutils-1.0-0.dll": "LGPL-2.1-or-later",
    "gstreamer-1.0-0.dll": "LGPL-2.1-or-later",
    "gstriff-1.0-0.dll": "LGPL-2.1-or-later",
    "gstrtp-1.0-0.dll": "LGPL-2.1-or-later",
    "gstrtsp-1.0-0.dll": "LGPL-2.1-or-later",
    "gstsdp-1.0-0.dll": "LGPL-2.1-or-later",
    "gsttag-1.0-0.dll": "LGPL-2.1-or-later",
    "gstvideo-1.0-0.dll": "LGPL-2.1-or-later",
    "gstwinrt-1.0-0.dll": "LGPL-2.1-or-later",
    # gstreamer_libs 1.28.5's pinned wheel contains bin/gstfft-1.0-0.dll
    # (wheel RECORD; its SHA-256 matches requirements-native-runtime.txt).
    # The exact 1.28.5 gst-plugins-base fft/meson.build compiles gstfft*.c
    # sources, whose headers grant LGPL-2.0-or-later, together with the
    # kiss_fft*.c sources marked BSD-3-Clause. Both source families are in
    # the DLL, so preserve both notices in its per-file expression.
    # Upstream: https://github.com/GStreamer/gstreamer/tree/1.28.5/subprojects/gst-plugins-base/gst-libs/gst/fft
    "gstfft-1.0-0.dll": "LGPL-2.0-or-later AND BSD-3-Clause",
    # --- Orc (Oil Runtime Compiler), a GStreamer subproject: BSD-2-Clause
    # per its own COPYING file.
    "orc-0.4-0.dll": "BSD-2-Clause",
    # --- Well-documented, unambiguous upstream licenses for the remaining
    # bundled support libraries. Each SPDX identifier below matches a term
    # present in the gstreamer_libs/gstreamer_plugins* wheels' own METADATA
    # aggregate (cross-referenced in the evidence memo), confirming these
    # are genuinely among the components those aggregates are describing.
    "bz2.dll": "bzip2-1.0.6",
    "ffi-7.dll": "MIT",
    # fontconfig 2.16.1 (version read from the debug path embedded in the
    # shipped DLL) is a CUMULATIVE multi-notice component, not a single
    # license -- this entry previously said HPND-sell-variant alone, which
    # named the primary grant and silently dropped four others (Codex
    # CC-WS5-PKG-004 round 2). Read from the exact upstream release commit's
    # COPYING (fdfc3445d1cc9c1c7e587fb2a1287871de16faf9), the terms are:
    #   HPND-sell-variant  -- the main grant (use/copy/modify/distribute/SELL,
    #                         no-advertising clause)
    #   MIT                -- src/fcfoundry.h (Juliusz Chroboczek)
    #   MIT-Modern-Variant -- src/fcatomic.h, src/fcmutex.h, both derived from
    #                         HarfBuzz; identifier confirmed by comparing the
    #                         header text to SPDX's, not assumed
    #   Unicode-TOU        -- fc-case/CaseFolding.txt, whose derived table is
    #                         compiled in
    # plus public-domain dedications (src/fcmd5.h, src/ftglue.[ch]) that carry
    # no conditions and so add no identifier. LicenseRef-Fontconfig-2.16.1 is
    # fontconfig's own COPYING bundled verbatim -- the operative notice, and
    # the thing that actually discharges the attribution requirement. It is
    # NOT a fifth dual-license election: nothing here is an "A OR B" choice.
    # Permissive throughout; neither GPL nor LGPL. Required by
    # cairo/pangocairo/pangoft2.
    "fontconfig-1.dll": FONTCONFIG_2_16_1_LICENSE,
    # FreeType is genuinely dual-licensed upstream: "FTL OR GPL-2.0" per its
    # own LICENSE.TXT, redistributor's choice. We elect the FTL branch -- a
    # valid choice under the dual license, not a guess about which branch was
    # "actually" compiled (the license terms are a copyright grant, not a
    # compile-time selectable feature), same rationale as the librtmp/cairo
    # elections below. See DUAL_LICENSE_ELECTIONS.
    "freetype-6.dll": "FTL",
    "fribidi-0.dll": "LGPL-2.1-or-later",
    "harfbuzz.dll": "MIT",
    "intl-8.dll": "LGPL-2.1-or-later",
    "libcrypto-3-x64.dll": "Apache-2.0",
    "libssl-3-x64.dll": "Apache-2.0",
    "libexpat.dll": "MIT",
    "nghttp2.dll": "MIT",
    "pango-1.0-0.dll": "LGPL-2.1-or-later",
    "pangocairo-1.0-0.dll": "LGPL-2.1-or-later",
    "pangoft2-1.0-0.dll": "LGPL-2.1-or-later",
    "pangowin32-1.0-0.dll": "LGPL-2.1-or-later",
    "pcre2-8-0.dll": "BSD-3-Clause",
    "pixman-1-0.dll": "MIT",
    "png16.dll": "Libpng",
    "psl-5.dll": "MIT",
    "soup-3.0-0.dll": "LGPL-2.1-or-later",
    # SQLite's own SPDX identifier for its public-domain dedication IS
    # literally "blessing" -- matches the "blessing" term present in the
    # gstreamer_libs wheel's own METADATA aggregate.
    "sqlite3-0.dll": "blessing",
    # Haivision SRT: MPL-2.0 per upstream LICENSE, matches "MPL-2.0" in the
    # gstreamer_plugins_libs wheel's own METADATA aggregate.
    "srt.dll": "MPL-2.0",
    "z-1.dll": "Zlib",
    # librtmp is genuinely dual-licensed upstream: "GPLv2 or LGPLv2.1" per
    # its own README, redistributor's choice. We elect the LGPL branch --
    # a valid choice under the dual license, not a guess about which
    # branch was "actually" compiled (the license terms are a copyright
    # grant, not a compile-time selectable feature).
    "rtmp-1.dll": "LGPL-2.1-or-later",
    # cairo/cairo-gobject are dual-licensed upstream: "LGPL-2.1-or-later OR
    # MPL-1.1", redistributor's choice (matches "MPL-1.1" present in the
    # gstreamer_libs wheel's own METADATA aggregate). We elect the LGPL
    # branch, same rationale as librtmp above.
    "cairo-2.dll": "LGPL-2.1-or-later",
    "cairo-gobject-2.dll": "LGPL-2.1-or-later",
    # --- Microsoft Visual C++ Redistributable files. THE KEY FINDING of
    # this investigation: these ship from the `gstreamer_ext_runtime`
    # wheel, whose OWN dist-info/METADATA declares
    # `License: LicenseRef-Proprietary` (confirmed both from the uv wheel
    # cache and from the built tree's own `licenses/gstreamer_ext_
    # runtime.txt`). They are Microsoft's proprietary redistributable
    # binaries under the VC++ Redistributable EULA -- not open source, and
    # specifically NOT LGPL-2.1-or-later. See the evidence memo's
    # "gstreamer_ext_runtime mislabel" section for the full writeup of why
    # this contradicts the current `DISTRIBUTION_LICENSE["gstreamer_ext_
    # runtime"]` entry in `runtime_manifest.py`.
    "msvcp140.dll": "LicenseRef-Microsoft-VCRedist",
    "vcruntime140.dll": "LicenseRef-Microsoft-VCRedist",
    "vcruntime140_1.dll": "LicenseRef-Microsoft-VCRedist",
}

#: `lib/gio/modules/*.dll` -- GIO extension modules. Both ship from
#: glib-networking (LGPL-2.1-or-later); the OpenSSL backend module links
#: OpenSSL (Apache-2.0) but the module FILE's own copyright is
#: glib-networking's.
_GIO_MODULE_LICENSE: Final[dict[str, str]] = {
    "giolibproxy.dll": "LGPL-2.1-or-later",
    "gioopenssl.dll": "LGPL-2.1-or-later",
}
SUPPORT_LIBRARY_LICENSE.update(_GIO_MODULE_LICENSE)


# ---------------------------------------------------------------------------
# Category 3 -- girepository-1.0/*.typelib
#
# Auto-generated introspection metadata; each typelib carries its
# originating project's license (g-ir-scanner extracts type information
# from that project's own headers/sources, it does not create new
# copyrightable material with a different license). Mapped to the
# corresponding SUPPORT_LIBRARY_LICENSE/PLUGIN_LICENSE entry where one
# exists; a handful of typelibs correspond to projects this tree ships NO
# compiled library for at all (GdkPixbuf, libxml2, the X11 typelibs, GL,
# Json, CudaGst) -- they are inert on Windows (nothing in the tree ever
# loads the backing DLL) but they are still shipped bytes that need a
# provenance entry. The two D-Bus typelibs were the same kind of inert
# bytes and were PRUNED entirely (owner-approved 2026-07-24) rather than
# shipped -- see the pruned-typelib note below and EXCLUDED_TYPELIB_BASENAMES.
# ---------------------------------------------------------------------------

TYPELIB_LICENSE: Final[dict[str, str]] = {
    "cairo-1.0.typelib": "LGPL-2.1-or-later",
    # This comment used to claim fontconfig-2.0.typelib was "deliberately
    # ABSENT from this table -- see UNRESOLVED_BASENAMES", sitting directly
    # above the line that maps it (Codex CC-WS5-PKG-004 round 2). The entry
    # was added when fontconfig's version was confirmed; the comment
    # describing its absence was never removed. It carries fontconfig's own
    # terms, so it takes fontconfig's own full cumulative expression -- the
    # same constant as fontconfig-1.dll, never a hand-copied duplicate that
    # could drift.
    "fontconfig-2.0.typelib": FONTCONFIG_2_16_1_LICENSE,
    # FreeType is dual-licensed upstream ("FTL OR GPL-2.0"); we elect the FTL
    # branch, same election as the freetype-6.dll entry in
    # SUPPORT_LIBRARY_LICENSE above -- this typelib carries FreeType's own
    # license, not a newly-generated one. See DUAL_LICENSE_ELECTIONS.
    "freetype2-2.0.typelib": "FTL",
    "GdkPixbuf-2.0.typelib": "LGPL-2.1-or-later",
    "GES-1.0.typelib": "LGPL-2.1-or-later",
    "Gio-2.0.typelib": "LGPL-2.1-or-later",
    "GIRepository-2.0.typelib": "LGPL-2.1-or-later",
    "GLib-2.0.typelib": "LGPL-2.1-or-later",
    "GModule-2.0.typelib": "LGPL-2.1-or-later",
    "GObject-2.0.typelib": "LGPL-2.1-or-later",
    "Gst-1.0.typelib": "LGPL-2.1-or-later",
    "GstAllocators-1.0.typelib": "LGPL-2.1-or-later",
    "GstAnalytics-1.0.typelib": "LGPL-2.1-or-later",
    "GstApp-1.0.typelib": "LGPL-2.1-or-later",
    "GstAudio-1.0.typelib": "LGPL-2.1-or-later",
    "GstBadAudio-1.0.typelib": "LGPL-2.1-or-later",
    "GstBase-1.0.typelib": "LGPL-2.1-or-later",
    "GstCheck-1.0.typelib": "LGPL-2.1-or-later",
    "GstCodecs-1.0.typelib": "LGPL-2.1-or-later",
    "GstController-1.0.typelib": "LGPL-2.1-or-later",
    "GstCuda-1.0.typelib": "LGPL-2.1-or-later",
    "GstD3D11-1.0.typelib": "LGPL-2.1-or-later",
    "GstD3D12-1.0.typelib": "LGPL-2.1-or-later",
    "GstDxva-1.0.typelib": "LGPL-2.1-or-later",
    "GstGL-1.0.typelib": "LGPL-2.1-or-later",
    "GstHip-1.0.typelib": "LGPL-2.1-or-later",
    "GstHipGL-1.0.typelib": "LGPL-2.1-or-later",
    "GstInsertBin-1.0.typelib": "LGPL-2.1-or-later",
    "GstMpegts-1.0.typelib": "LGPL-2.1-or-later",
    "GstMse-1.0.typelib": "LGPL-2.1-or-later",
    "GstNet-1.0.typelib": "LGPL-2.1-or-later",
    "GstPbutils-1.0.typelib": "LGPL-2.1-or-later",
    "GstPlay-1.0.typelib": "LGPL-2.1-or-later",
    "GstPlayer-1.0.typelib": "LGPL-2.1-or-later",
    "GstRtp-1.0.typelib": "LGPL-2.1-or-later",
    "GstRtsp-1.0.typelib": "LGPL-2.1-or-later",
    "GstRtspServer-1.0.typelib": "LGPL-2.1-or-later",
    "GstSdp-1.0.typelib": "LGPL-2.1-or-later",
    "GstTag-1.0.typelib": "LGPL-2.1-or-later",
    "GstTranscoder-1.0.typelib": "LGPL-2.1-or-later",
    "GstValidate-1.0.typelib": "LGPL-2.1-or-later",
    "GstVideo-1.0.typelib": "LGPL-2.1-or-later",
    "GstWebRTC-1.0.typelib": "LGPL-2.1-or-later",
    "HarfBuzz-0.0.typelib": "MIT",
    "Json-1.0.typelib": "LGPL-2.1-or-later",
    "libxml2-2.0.typelib": "MIT",
    "Pango-1.0.typelib": "LGPL-2.1-or-later",
    "PangoCairo-1.0.typelib": "LGPL-2.1-or-later",
    "PangoFT2-1.0.typelib": "LGPL-2.1-or-later",
    "Soup-3.0.typelib": "LGPL-2.1-or-later",
    "win32-1.0.typelib": "LGPL-2.1-or-later",
    "xfixes-4.0.typelib": "MIT",
    "xft-2.0.typelib": "MIT",
    "xlib-2.0.typelib": "MIT",
    "xrandr-1.3.typelib": "MIT",
    "CudaGst-1.0.typelib": "LGPL-2.1-or-later",
    # DBus-1.0.typelib / DBusGLib-1.0.typelib were PRUNED from the shipped
    # closure (owner-approved 2026-07-24 -- see
    # `.agent-runs/native-windows/ws5-installer/OWNER-DECISION-licensing-dispositions.md`).
    # Neither corresponding DLL (libdbus / dbus-glib) ever shipped in bin/, so
    # they were inert, GPL-adjacent bytes ("AFL-2.1 OR GPL-2.0-or-later"
    # upstream) that nothing this closure runs could load. They are excluded at
    # copy time by `EXCLUDED_TYPELIB_BASENAMES` in
    # `scripts/build_native_runtime_closure.py`, so they are no longer shipped
    # and therefore no longer classified here (a license entry for an unshipped
    # file would be a claim about a file not in the tree). AFL-2.1 was elected
    # only by these two, so its bundled license text was removed with them.
    "GL-1.0.typelib": "LGPL-2.1-or-later",
}

#: `fontconfig-1.dll` (bin/) and its typelib counterpart are the one
#: genuine "we could not confirm this with the same rigor as everything
#: else" gap this investigation found. fontconfig has no runtime
#: license-query API (unlike GStreamer plugins or FFmpeg), and its
#: upstream license text is not a registered SPDX identifier this
#: investigation could match with confidence. Named explicitly per AC7
#: rather than silently guessed. See evidence memo "Unresolved" section.
#: RESOLVED 2026-07-23, so this set is now empty -- kept (rather than deleted)
#: because it is the mechanism AC7 depends on: any future file whose provenance
#: cannot be confirmed goes in here and halts the build, and an empty set is a
#: statement that there are currently none, not that the concept was dropped.
#:
#: fontconfig was the one open case. Resolved with real evidence rather than a
#: guess: the shipped binary self-identifies as fontconfig 2.16.1 (debug path
#: `fontconfig-2.16.1\\b\\src\\fontconfig-1.pdb` embedded in the DLL), and
#: upstream's COPYING for that project reads "Permission to use, copy, modify,
#: distribute, and SELL this software and its documentation for any purpose is
#: hereby granted without fee ... and that the name of the author(s) not be used
#: in advertising or publicity". The "and sell" grant plus the no-advertising
#: clause is precisely SPDX `HPND-sell-variant` -- permissive, and categorically
#: neither GPL nor LGPL. It ships because it is genuinely required: cairo-2.dll,
#: pangocairo-1.0-0.dll and pangoft2-1.0-0.dll all import it (verified against
#: the built tree), so it cannot simply be pruned.
UNRESOLVED_BASENAMES: Final[frozenset[str]] = frozenset()


# ---------------------------------------------------------------------------
# Dual-license elections
#
# Every basename below names a shipped file whose UPSTREAM project offers a
# genuine choice of license (a redistributor election), not a single license
# this investigation "discovered". The value recorded for it in
# PLUGIN_LICENSE / SUPPORT_LIBRARY_LICENSE / TYPELIB_LICENSE above is the
# ELECTED branch. This registry exists so the election itself is a checkable
# fact, not just prose in a comment a future reader could miss (CC-WS5-
# PKG-005: FreeType was originally recorded with no such disclosure at all,
# as though FTL were simply its license, unlike the other three elections
# below which WERE disclosed as elections from the start).
# ---------------------------------------------------------------------------

DUAL_LICENSE_ELECTIONS: Final[frozenset[str]] = frozenset(
    {
        # librtmp: "GPLv2 or LGPLv2.1" upstream README; elected the LGPL
        # branch (SUPPORT_LIBRARY_LICENSE).
        "rtmp-1.dll",
        # cairo / cairo-gobject: "LGPL-2.1-or-later OR MPL-1.1" upstream;
        # elected the LGPL branch (SUPPORT_LIBRARY_LICENSE).
        "cairo-2.dll",
        "cairo-gobject-2.dll",
        # FreeType: "FTL OR GPL-2.0" per its own LICENSE.TXT; elected the FTL
        # branch (SUPPORT_LIBRARY_LICENSE, TYPELIB_LICENSE).
        "freetype-6.dll",
        "freetype2-2.0.typelib",
        # The two D-Bus typelibs ("AFL-2.1 OR GPL-2.0-or-later" upstream) were
        # PRUNED from the shipped closure (owner-approved 2026-07-24) rather
        # than elected: they were inert bytes no shipped DLL could load. With
        # the files gone there is no shipped file to record an election for, so
        # they were removed from this registry too. See the pruned-typelib note
        # in TYPELIB_LICENSE above and OWNER-DECISION-licensing-dispositions.md.
    }
)


# ---------------------------------------------------------------------------
# Category 4 -- CivicCast-generated BOM/trust artifacts
#
# These are original text written by `build_native_runtime_closure.py`
# itself (LICENSE-BOM.md, runtime-manifest.json, SHA256SUMS, and the
# `licenses/` upstream-notice directory it generates) -- not redistributed
# upstream binaries, so they are governed by the CivicCast repository's own
# Apache-2.0, not a `DISTRIBUTION_LICENSE` entry.
# ---------------------------------------------------------------------------

GENERATED_ARTIFACT_LICENSE: Final[str] = "Apache-2.0"

#: Exact root-relative paths (not just basenames -- "README.md" alone would
#: be too generic to safely prefix-match) for the three top-level BOM
#: siblings. `licenses/` itself is matched by directory prefix in
#: `classify_shipped_file`.
GENERATED_ARTIFACT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "LICENSE-BOM.md",
        "runtime-manifest.json",
        "SHA256SUMS",
    }
)


# ---------------------------------------------------------------------------
# Category 5 -- whole-directory prefixes
#
# `python/gi/` covers every file the gstreamer_python wheel contributes
# (LGPL-2.1-or-later in its own METADATA) regardless of that file's
# individual basename -- many of them (`__init__.py`, `types.py`,
# `module.py`) are far too generic a basename to safely match on their own.
# `licenses/` covers the generated upstream-notice directory (Category 4).
# ---------------------------------------------------------------------------

PATH_PREFIX_LICENSE: Final[tuple[tuple[str, str], ...]] = (
    ("python/gi/", "LGPL-2.1-or-later"),
    ("licenses/", GENERATED_ARTIFACT_LICENSE),
)


# ---------------------------------------------------------------------------
# classify_shipped_file
# ---------------------------------------------------------------------------


def classify_shipped_file(path: str) -> str | None:
    """Classify one shipped-tree path (forward-slash, relative to the tree
    root -- the `FileEntry.path` convention) to its evidence-backed SPDX
    license identifier.

    Returns `None` when this investigation did not confirm a license for
    ``path`` -- AC7 forbids guessing, so an unresolved file must come back
    `None`, never a default. Basenames listed in `GPL_EXCLUDED_PLUGIN_
    BASENAMES` or `UNRESOLVED_BASENAMES` always resolve to `None` here by
    construction (they are simply absent from every lookup table).

    Resolution order: exact top-level artifact path, then directory-prefix
    rules (whole components too generically-named to basename-match
    safely), then basename lookup against the plugin/support-library/
    typelib tables. Basename lookup is safe for everything past the prefix
    stage because the plugin, support-library, and typelib tables are each
    scoped to their own known directory by construction of the built tree
    (`lib/gstreamer-1.0/`, `bin/` + `lib/gio/modules/`, `lib/girepository-
    1.0/` respectively) -- a basename collision across those directories
    would itself be a build anomaly worth investigating, not something this
    classifier should paper over.
    """
    if path in GENERATED_ARTIFACT_PATHS:
        return GENERATED_ARTIFACT_LICENSE

    for prefix, license_ in PATH_PREFIX_LICENSE:
        if path.startswith(prefix):
            return license_

    basename = path.rsplit("/", 1)[-1]

    if basename in UNRESOLVED_BASENAMES or basename in GPL_EXCLUDED_PLUGIN_BASENAMES:
        return None

    if basename in PLUGIN_LICENSE:
        return PLUGIN_LICENSE[basename]
    if basename in SUPPORT_LIBRARY_LICENSE:
        return SUPPORT_LIBRARY_LICENSE[basename]
    if basename in TYPELIB_LICENSE:
        return TYPELIB_LICENSE[basename]

    return None


# ---------------------------------------------------------------------------
# is_gpl_license -- the explicit GPL-detection predicate
# ---------------------------------------------------------------------------

#: Matches SPDX-expression tokens: a letter, then letters/digits/dot/plus/
#: dash. Used to split a (possibly `AND`/`OR`-combined) license expression
#: into individual license-id tokens without a naive substring test --
#: `"GPL" in "LGPL-2.1-or-later"` is `True` and would be a false positive;
#: tokenizing first and checking whole-token prefixes is not fooled by
#: that, because the token is `"LGPL-2.1-or-later"`, which does not START
#: with `"GPL-"`.
_LICENSE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9.+-]*")


def is_gpl_license(license_expression: str) -> bool:
    """Does ``license_expression`` name or offer a GPL (not LGPL, not AGPL)
    license?

    Catches bare ``"GPL"``, versioned identifiers (``"GPL-2.0-or-later"``,
    ``"GPL-3.0-only"``, ...), and a GPL branch inside an `AND`/`OR`-combined
    SPDX expression (e.g. librtmp's own dual-licensed ``"LGPL-2.1-or-later
    OR GPL-2.0-or-later"``). Never true for `LGPL-*` or `AGPL-*` tokens --
    those are different license families that happen to share the `GPL`
    substring, not GPL itself.
    """
    normalized = license_expression.upper()
    if (
        "GNU GENERAL PUBLIC LICENSE" in normalized
        and "GNU LESSER GENERAL PUBLIC LICENSE" not in normalized
    ):
        return True
    for license_term in _LICENSE_TOKEN_RE.findall(normalized):
        if (
            license_term == "GPL"
            or license_term.startswith("GPL-")
            or re.fullmatch(r"GPLV\d+(?:\.\d+)?", license_term)
        ):
            return True
    return False


#: SPDX expression OPERATORS, which are not license identifiers and so never
#: need a bundled text of their own. Uppercase per the SPDX spec.
_SPDX_OPERATORS: Final[frozenset[str]] = frozenset({"AND", "OR", "WITH"})


def license_identifiers_in(license_expression: str) -> tuple[str, ...]:
    """Every distinct license identifier named by ``license_expression``, sorted.

    A single identifier (`"MIT"`) yields one; a cumulative expression
    (fontconfig's `"HPND-sell-variant AND MIT AND ..."`) yields each
    component. This exists because D3's obligation is per-NOTICE, not
    per-file: an expression that names five licenses requires five texts to
    be bundled, and treating the whole expression as one opaque key would
    silently ship four of them missing -- while still looking correct,
    because the BOM string itself would be accurate.

    Uses the same tokenizer as `is_gpl_license` so the two can never disagree
    about what an expression contains.
    """
    return tuple(
        sorted(
            {
                token
                for token in _LICENSE_TOKEN_RE.findall(license_expression)
                if token not in _SPDX_OPERATORS
            }
        )
    )


# ---------------------------------------------------------------------------
# Category 6 -- the native-server-binaries pack (PostgreSQL 17 +
# TSDuck; WP2 Core-pack builder, `scripts/build_native_server_pack.py`).
# NATS JetStream was removed from the product (owner decision 2026-08-20;
# see ADR 0023, which supersedes ADR 0001) -- nats-server.exe is no longer
# part of this pack.
#
# A SEPARATE table/function from `classify_shipped_file` above rather than
# entries merged into its basename tables: that function's docstring and
# resolution order are scoped specifically to the GStreamer/FFmpeg media
# closure's own directory layout (`lib/gstreamer-1.0/`, `lib/gio/modules/`,
# `lib/girepository-1.0/`), and at least one basename genuinely recurs here
# with the SAME value for an unrelated reason (`libcrypto-3-x64.dll` /
# `libssl-3-x64.dll` are OpenSSL 3.x in BOTH trees, both Apache-2.0) -- true
# today, but merging the namespaces would make a FUTURE same-basename,
# different-license collision between the two unrelated trees silently
# resolve to whichever table happens to be checked first instead of failing
# loud. Keeping the server pack's own resolver keeps that impossible by
# construction: `classify_server_pack_file` only ever sees paths from the
# server pack's own build (`scripts/build_native_server_pack.py`'s
# `sources` dict), never the media closure's.
#
# Evidence for every entry below: extracted and inspected directly from the
# exact pinned upstream archives named in `native-windows-runtime-
# dependencies.lock.json` (PostgreSQL 17.10-2 Windows x64 binaries,
# TSDuck v3.44-4676 Win64 Portable) --
# verbatim command output and per-notice SHA-256 recorded in
# `.agent-runs/native-windows/ws5-installer/evidence/
# wp2-core-pack-2026-07-29.md`. Deny-by-default (AC7, same as
# `classify_shipped_file` above): `classify_server_pack_file` returns
# `None` for anything not confirmed here, never a guess.
# ---------------------------------------------------------------------------

#: PostgreSQL 17's own bin/lib payload. `server_license.txt` (the
#: PostgreSQL License -- liberal, BSD/MIT-like, "Portions Copyright (c)
#: 1996-2025, The PostgreSQL Global Development Group") covers the core
#: server, client tools, and every `lib/*.dll` loadable extension shipped
#: from the SAME archive (`btree_gist.dll` -- the one extension the schema
#: actually loads, `civiccast/schedule/migrations/versions/
#: 0003_create_schedule_items_table.py`'s `CREATE EXTENSION IF NOT EXISTS
#: btree_gist` -- carries no separate license of its own; it ships from the
#: same PostgreSQL contrib tree under the same license as `postgres.exe`
#: itself). The remaining `bin/*.dll` entries are PostgreSQL's OWN bundled
#: third-party runtime dependencies, confirmed individually against
#: `commandlinetools_3rd_party_licenses.txt` (shipped inside the SAME
#: archive, alongside `server_license.txt`) -- never guessed from the
#: upstream project's reputation.
SERVER_PACK_BASENAME_LICENSE: Final[dict[str, str]] = {
    # --- PostgreSQL core + client tools + the one loaded contrib extension:
    # PostgreSQL License, per `server_license.txt` in the same archive.
    "initdb.exe": "PostgreSQL",
    "postgres.exe": "PostgreSQL",
    "pg_ctl.exe": "PostgreSQL",
    "pg_dump.exe": "PostgreSQL",
    "pg_dumpall.exe": "PostgreSQL",
    "pg_restore.exe": "PostgreSQL",
    "psql.exe": "PostgreSQL",
    "libpq.dll": "PostgreSQL",
    "btree_gist.dll": "PostgreSQL",
    # --- PostgreSQL core lib/ runtime modules (encoding-conversion family +
    # plpgsql + dict_snowball), added after Sandbox matrix row 1 run 2
    # proved initdb's bootstrap loads them (`$libdir/utf8_and_win` FATAL).
    # All are PostgreSQL-source core modules under the same
    # `server_license.txt` PostgreSQL License as the server itself.
    "cyrillic_and_mic.dll": "PostgreSQL",
    "euc2004_sjis2004.dll": "PostgreSQL",
    "euc_cn_and_mic.dll": "PostgreSQL",
    "euc_jp_and_sjis.dll": "PostgreSQL",
    "euc_kr_and_mic.dll": "PostgreSQL",
    "euc_tw_and_big5.dll": "PostgreSQL",
    "latin2_and_win1250.dll": "PostgreSQL",
    "latin_and_mic.dll": "PostgreSQL",
    "utf8_and_big5.dll": "PostgreSQL",
    "utf8_and_cyrillic.dll": "PostgreSQL",
    "utf8_and_euc2004.dll": "PostgreSQL",
    "utf8_and_euc_cn.dll": "PostgreSQL",
    "utf8_and_euc_jp.dll": "PostgreSQL",
    "utf8_and_euc_kr.dll": "PostgreSQL",
    "utf8_and_euc_tw.dll": "PostgreSQL",
    "utf8_and_gb18030.dll": "PostgreSQL",
    "utf8_and_gbk.dll": "PostgreSQL",
    "utf8_and_iso8859.dll": "PostgreSQL",
    "utf8_and_iso8859_1.dll": "PostgreSQL",
    "utf8_and_johab.dll": "PostgreSQL",
    "utf8_and_sjis.dll": "PostgreSQL",
    "utf8_and_sjis2004.dll": "PostgreSQL",
    "utf8_and_uhc.dll": "PostgreSQL",
    "utf8_and_win.dll": "PostgreSQL",
    "plpgsql.dll": "PostgreSQL",
    "dict_snowball.dll": "PostgreSQL",
    # --- PostgreSQL's bundled third-party runtime libraries, each entry in
    # `commandlinetools_3rd_party_licenses.txt` (section headers: "zstd
    # license", "gettext, libiconv, pthreads license", "openssl license",
    # "icu license", "liblz4 license", "libpq license", "libxml2, libxslt
    # license", "zlib license").
    "libzstd.dll": "BSD-3-Clause",
    "libintl-9.dll": "LGPL-2.1-or-later",  # gettext
    "libiconv-2.dll": "LGPL-2.1-or-later",
    "libwinpthread-1.dll": "LGPL-2.1-or-later",  # winpthreads (MinGW-w64)
    "libcrypto-3-x64.dll": "Apache-2.0",  # OpenSSL 3.x
    "libssl-3-x64.dll": "Apache-2.0",
    "icudt67.dll": "ICU",
    "icuin67.dll": "ICU",
    "icuuc67.dll": "ICU",
    "liblz4.dll": "BSD-2-Clause",
    "libxml2.dll": "MIT",
    "zlib1.dll": "Zlib",
    # --- TSDuck: BSD-2-Clause per its own `LICENSE.txt`. `tsp.exe`,
    # `tscore.dll`, and `tsduck.dll` are TSDuck's own first-party code.
    # TSDuck's bundled-third-party notices (`OTHERS.txt`: DTAPI [proprietary,
    # Dektec], LIBSRT [MPL-2.0], LIBRIST [BSD-2-Clause], LibVatek
    # [BSD-2-Clause], Small Deflate [MIT/public domain]) are confirmed to
    # NOT apply to this shipped subset: a direct PE import-closure walk of
    # `tsp.exe` plus every plugin DLL this pack ships resolves to exactly
    # `tscore.dll` + `tsduck.dll` (plus only OS-provided DLLs -- QUARTZ,
    # WININET, WINUSB, WinSCard); none of the SRT/RIST/Dektec/VATek plugin
    # DLLs that would pull those third-party components in
    # (`tsplugin_srt.dll`, `tsplugin_rist.dll`, `tsplugin_dektec.dll`,
    # `tsplugin_vatek.dll`) are part of this pack's plugin subset (see
    # `scripts/build_native_server_pack.py`'s `TSDUCK_BIN_PINS`), so their
    # bundled notices would be provenance for bytes this pack does not ship.
    "tsp.exe": "BSD-2-Clause",
    "tscore.dll": "BSD-2-Clause",
    "tsduck.dll": "BSD-2-Clause",
    # --- The exact 4 TSDuck processor plugins `civiccast/egress/ts_relay.py`
    # (`continuity`, `pcradjust`), `civiccast/egress/compliance.py`
    # (`until`, `analyze`), and `civiccast/alerting/self_test.py` (`analyze`)
    # invoke via `tsp -P <name>`. Each plugin DLL is TSDuck's own first-party
    # code (same BSD-2-Clause as `tsp.exe`) -- `ip`, `file`, and `drop`
    # (also used by the same call sites) are NOT separate files: TSDuck
    # compiles those three fundamental I/O plugins directly into
    # `tsduck.dll`/`tscore.dll`, confirmed by their absence from the
    # upstream ZIP's `bin/tsplugin_*.dll` listing.
    "tsplugin_analyze.dll": "BSD-2-Clause",
    "tsplugin_continuity.dll": "BSD-2-Clause",
    "tsplugin_pcradjust.dll": "BSD-2-Clause",
    "tsplugin_until.dll": "BSD-2-Clause",
    # --- TSDuck's own bundled .names/.xml configuration/data files, shipped
    # alongside tsp.exe in the SAME upstream bin/ directory (TSDuck resolves
    # them relative to tsp.exe's own path on Windows) -- TSDuck first-party
    # data under the identical BSD-2-Clause LICENSE.txt as tsp.exe itself,
    # not third-party notices from OTHERS.txt (see scripts/
    # build_native_server_pack.py's TSDUCK_DATA_PINS comment for the exact
    # enumeration/derivation).
    "tscore.ip.names": "BSD-2-Clause",
    "tscore.keytable.model.xml": "BSD-2-Clause",
    "tscore.monitor.model.xml": "BSD-2-Clause",
    "tscore.monitor.xml": "BSD-2-Clause",
    "tscore.time.model.xml": "BSD-2-Clause",
    "tscore.time.xml": "BSD-2-Clause",
    "tsduck.channels.model.xml": "BSD-2-Clause",
    "tsduck.dektec.names": "BSD-2-Clause",
    "tsduck.dtv.names": "BSD-2-Clause",
    "tsduck.etuner.model.xml": "BSD-2-Clause",
    "tsduck.hfbands.model.xml": "BSD-2-Clause",
    "tsduck.hfbands.xml": "BSD-2-Clause",
    "tsduck.hides.names": "BSD-2-Clause",
    "tsduck.lnbs.model.xml": "BSD-2-Clause",
    "tsduck.lnbs.xml": "BSD-2-Clause",
    "tsduck.oui.names": "BSD-2-Clause",
    "tsduck.tables.model.xml": "BSD-2-Clause",
}

#: Whole-directory / generated-file prefixes for the server pack, mirroring
#: `PATH_PREFIX_LICENSE`'s shape above. `share/timezone/` and
#: `share/timezonesets/` are PostgreSQL's bundled IANA time zone database
#: (public-domain data, no copyright notice of its own -- PostgreSQL
#: redistributes it verbatim as part of the SAME PostgreSQL-licensed
#: archive); `share/extension/btree_gist*` and the remaining pinned
#: `share/*.sql`/`share/*.sample`/`share/postgres.bki` bootstrap files carry
#: the same PostgreSQL License as the binaries in the same archive.
#: `licenses/` is this pack builder's own generated upstream-notice
#: directory (Category 4's `GENERATED_ARTIFACT_LICENSE` convention).
SERVER_PACK_PATH_PREFIX_LICENSE: Final[tuple[tuple[str, str], ...]] = (
    ("share/timezone/", "PostgreSQL"),
    ("share/timezonesets/", "PostgreSQL"),
    ("share/extension/", "PostgreSQL"),
    ("share/", "PostgreSQL"),
    ("licenses/", GENERATED_ARTIFACT_LICENSE),
    ("notices/", GENERATED_ARTIFACT_LICENSE),
)


def classify_server_pack_file(path: str) -> str | None:
    """Classify one `native-server-binaries` pack path (forward-slash,
    relative to the pack payload root) to its evidence-backed SPDX license
    identifier.

    Same AC7 contract as `classify_shipped_file`: an unconfirmed path
    returns `None`, never a guessed default. Resolution order: directory
    prefix (`SERVER_PACK_PATH_PREFIX_LICENSE`), then basename
    (`SERVER_PACK_BASENAME_LICENSE`) -- basename lookup is safe here because
    every basename this table names was individually confirmed against a
    real upstream license/notice file (see the evidence comments above),
    not assumed safe by directory location the way a prefix rule is.
    """
    for prefix, license_ in SERVER_PACK_PATH_PREFIX_LICENSE:
        if path.startswith(prefix):
            return license_

    basename = path.rsplit("/", 1)[-1]
    return SERVER_PACK_BASENAME_LICENSE.get(basename)


# Defense-in-depth, at import time: zero GPL/AGPL tolerance for the server
# pack (task instruction) means a GPL-family entry in the table above must
# never even load successfully, let alone silently ship. Uses the SAME
# `is_gpl_license` predicate the rest of this module already trusts, so
# there is exactly one place "is this GPL" is decided.
for _server_pack_basename, _server_pack_license in SERVER_PACK_BASENAME_LICENSE.items():
    if is_gpl_license(_server_pack_license):
        raise RuntimeError(
            "civiccast.native.runtime_licenses: GPL-family license recorded for "
            f"native-server-binaries pack entry {_server_pack_basename!r} "
            f"({_server_pack_license!r}) -- zero GPL/AGPL tolerance for this pack"
        )
del _server_pack_basename, _server_pack_license


# ---------------------------------------------------------------------------
# Category 7 -- the native-ffmpeg-runtime pack (the FFmpeg COMMAND-LINE tools,
# `scripts/build_native_ffmpeg_pack.py`)
#
# Distinct from Category 2's `SUPPORT_LIBRARY_LICENSE`, which covers the
# `avcodec-61.dll`/`avutil-59.dll`/... family the PyAV/GStreamer WHEELS ship
# inside the app-payload media closure. This pack ships a DIFFERENT upstream
# artifact entirely: BtbN's `win64-lgpl-shared` FFmpeg build, whose libraries
# carry different soname majors (`avcodec-62.dll`, `avutil-60.dll`, ...) AND a
# different license (LGPL-3.0-or-later, because that build is configured
# `--enable-version3`; the wheels' build is not). A shared basename table
# across the two would therefore be actively wrong, not merely untidy -- the
# same reasoning Category 6's header already gives for keeping the server
# pack's namespace separate.
#
# EVIDENCE, per file, from the binaries themselves rather than from the
# upstream project's reputation:
#
#   * `ffmpeg.exe -L` / `ffprobe.exe -L` (run against the exact extracted,
#     hash-verified archive this builder packs) print, verbatim: "ffmpeg is
#     free software; you can redistribute it and/or modify it under the terms
#     of the GNU Lesser General Public License as published by the Free
#     Software Foundation; either version 3 of the License, or (at your
#     option) any later version." That is the binaries' OWN self-report of
#     LGPL-3.0-or-later.
#   * The same binaries' `-version` configuration string carries
#     `--enable-version3` and carries NEITHER `--enable-gpl` NOR
#     `--enable-nonfree`, and explicitly disables every GPL-only external
#     encoder the build could otherwise have linked (`--disable-libx264
#     --disable-libx265 --disable-libxvid --disable-libxavs2
#     --disable-libdavs2 --disable-librubberband --disable-libvidstab
#     --disable-frei0r --disable-avisynth`) plus the nonfree
#     `--disable-libfdk-aac`. A GPL branch in this artifact is therefore
#     excluded by the build configuration itself, not merely by the filename
#     of the archive.
#   * `native-windows-runtime-dependencies.lock.json`'s `ffmpeg` artifact
#     independently records `spdx_license: "LGPL-3.0-or-later"` for the SAME
#     hash-pinned archive -- a reviewed second source agreeing with the
#     binaries' self-report.
#
# The av*/sw* libraries and the two executables all come out of that single
# `--enable-version3` FFmpeg build tree and are covered by the one LGPL-3.0
# text the archive itself ships (packed here as
# `licenses/ffmpeg/LICENSE.txt`), so they share one identifier.
#
# SCOPE NOTE, recorded rather than papered over (the same honesty posture
# `build_native_runtime_closure.render_license_notices_readme`'s "What this
# directory is NOT" section takes): the identifier below governs the FFmpeg
# code in these files. That build also STATICALLY links a number of external
# libraries under their own licenses (libopenh264, libaom, libopus, libvpx,
# libmp3lame, libwebp, ... -- the complete enabled set is enumerated verbatim
# in the binaries' own `-version` configuration string, which the pack's
# generated NOTICE reproduces). BtbN's archive ships no per-dependency license
# texts of its own, so this pack does not claim per-dependency provenance it
# does not have; it names the gap in the NOTICE instead. Establishing the full
# static-dependency BOM for this artifact is tracked work, not something this
# table silently asserts.
# ---------------------------------------------------------------------------

#: The exact minimal PE closure of `ffmpeg.exe` + `ffprobe.exe` in BtbN's
#: `win64-lgpl-shared` build (nine files, resolved by a real recursive
#: `pefile` import-table walk -- see `scripts/build_native_ffmpeg_pack.py`'s
#: `FFMPEG_BIN_PINS`). Every one is FFmpeg's own build output.
FFMPEG_PACK_BASENAME_LICENSE: Final[dict[str, str]] = {
    "ffmpeg.exe": "LGPL-3.0-or-later",
    "ffprobe.exe": "LGPL-3.0-or-later",
    "avcodec-62.dll": "LGPL-3.0-or-later",
    "avdevice-62.dll": "LGPL-3.0-or-later",
    "avfilter-11.dll": "LGPL-3.0-or-later",
    "avformat-62.dll": "LGPL-3.0-or-later",
    "avutil-60.dll": "LGPL-3.0-or-later",
    "swresample-6.dll": "LGPL-3.0-or-later",
    "swscale-9.dll": "LGPL-3.0-or-later",
}

#: Generated-directory prefixes, mirroring `SERVER_PACK_PATH_PREFIX_LICENSE`'s
#: shape exactly: `licenses/` holds the verbatim upstream license text this
#: builder copies out of the archive, `notices/` this builder's own generated
#: NOTICE.
FFMPEG_PACK_PATH_PREFIX_LICENSE: Final[tuple[tuple[str, str], ...]] = (
    ("licenses/", GENERATED_ARTIFACT_LICENSE),
    ("notices/", GENERATED_ARTIFACT_LICENSE),
)


def classify_ffmpeg_pack_file(path: str) -> str | None:
    """Classify one `native-ffmpeg-runtime` pack path (forward-slash,
    relative to the pack payload root) to its evidence-backed SPDX license
    identifier.

    Same AC7 contract as `classify_shipped_file`/`classify_server_pack_file`:
    an unconfirmed path returns `None`, never a guessed default. Resolution
    order matches `classify_server_pack_file`'s: directory prefix
    (`FFMPEG_PACK_PATH_PREFIX_LICENSE`), then basename
    (`FFMPEG_PACK_BASENAME_LICENSE`).
    """
    for prefix, license_ in FFMPEG_PACK_PATH_PREFIX_LICENSE:
        if path.startswith(prefix):
            return license_

    basename = path.rsplit("/", 1)[-1]
    return FFMPEG_PACK_BASENAME_LICENSE.get(basename)


# Defense-in-depth, at import time, exactly as Category 6 already does for the
# server pack -- and load-bearing here for a different reason: FFmpeg ships in
# BOTH a GPL and an LGPL upstream flavour under near-identical archive names,
# and the owner-settled constraint is the LGPL one. If a future edit ever
# retargets this pack at a `win64-gpl*` archive, the licence recorded here
# would have to become GPL-family -- and this guard makes that edit fail at
# IMPORT, before any pack is built, rather than at review time or not at all.
# `is_gpl_license` is deliberately False for `LGPL-*`, so the correct LGPL
# artifact passes cleanly.
for _ffmpeg_pack_basename, _ffmpeg_pack_license in FFMPEG_PACK_BASENAME_LICENSE.items():
    if is_gpl_license(_ffmpeg_pack_license):
        raise RuntimeError(
            "civiccast.native.runtime_licenses: GPL-family license recorded for "
            f"native-ffmpeg-runtime pack entry {_ffmpeg_pack_basename!r} "
            f"({_ffmpeg_pack_license!r}) -- this pack must carry the LGPL FFmpeg "
            "build (owner-settled: no GPL)"
        )


# ---------------------------------------------------------------------------
# Category 8 -- the native-cuda-runtime pack (cuBLAS + cuDNN Windows runtime
# DLLs, `scripts/build_native_cuda_pack.py`)
#
# Owner ruling (Scott Converse, 2026-08-15): capable stations get GPU caption
# acceleration -- `station_runtime.resolve_cuda_bin_dir`'s presence gate
# already ships (a prior work package), but nothing built the pack it looks
# for. This pack is that builder: it downloads the pinned
# `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` PyPI wheels (the SAME wheels a
# `pip install torch`-style CUDA setup would pull) and repacks the two
# wheels' own `nvidia/cublas/bin/`/`nvidia/cudnn/bin/` DLL trees, flattened,
# as the signed `native-cuda-runtime` component.
#
# Distinct from every other category here: neither cuBLAS nor cuDNN is open
# source. Both are proprietary NVIDIA binaries distributed under NVIDIA's own
# end-user license agreements (the CUDA Toolkit EULA for cuBLAS, the cuDNN
# Supplement to that EULA for cuDNN) -- not an SPDX-registered open-source
# identifier. Recorded here as `LicenseRef-*` (SPDX's own convention for a
# license with no registered identifier) so `classify_cuda_pack_file` still
# gives AC7 a confirmed, evidence-backed answer instead of leaving every
# shipped DLL an unresolved provenance gap.
#
# BASENAME-PREFIX rule, not an exact per-file table like
# `FFMPEG_PACK_BASENAME_LICENSE`'s derived nine-file closure: this pack ships
# EVERY DLL the pinned wheels carry under their `bin/` directories (the
# builder's own doc comment: "extracts every DLL", not a minimized PE-import
# closure), so the exact shipped filename SET is only known from the real
# wheels at real build time -- re-pinning a wheel version can add or drop a
# file this table would otherwise need editing to keep up with, silently. A
# prefix rule is the evidence-backed answer for both families instead:
# NVIDIA's own DLL naming convention names every cuBLAS runtime file
# `cublas*` (`cublas64_12.dll`, `cublasLt64_12.dll`, ...) -- plus the nvBLAS
# drop-in `nvblas*` (`nvblas64_12.dll`), shipped in the SAME cuBLAS wheel under
# the SAME CUDA Toolkit EULA, so it is a third recognized prefix, not a third
# license -- and every cuDNN
# runtime file `cudnn*` (`cudnn64_9.dll`, `cudnn_ops64_9.dll`,
# `cudnn_cnn64_9.dll`, `cudnn_adv64_9.dll`, `cudnn_graph64_9.dll`,
# `cudnn_heuristic64_9.dll`, `cudnn_engines_precompiled64_9.dll`,
# `cudnn_engines_runtime_compiled64_9.dll`, ...) -- confirmed against both
# wheels' published file listings at review time. A file that matches
# NONE of these prefixes is exactly what AC7 exists to catch: something this
# investigation did not confirm, and `classify_cuda_pack_file` returns `None`
# for it rather than guessing (the builder's own
# `_require_full_license_provenance` gate refuses the build on that `None`,
# the same fail-closed posture Category 6/7's guards already take).
# ---------------------------------------------------------------------------

#: The CUDA Toolkit EULA, which governs the cuBLAS runtime shipped by the
#: `nvidia-cublas-cu12` wheel. Reference: https://docs.nvidia.com/cuda/eula
CUDA_TOOLKIT_EULA_LICENSE: Final[str] = "LicenseRef-NVIDIA-CUDA-EULA"

#: The cuDNN Supplement to the CUDA Toolkit EULA, which governs the cuDNN
#: runtime shipped by the `nvidia-cudnn-cu12` wheel. Reference:
#: https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html
CUDNN_EULA_LICENSE: Final[str] = "LicenseRef-NVIDIA-cuDNN-EULA"

#: Generated-directory prefixes, mirroring `FFMPEG_PACK_PATH_PREFIX_LICENSE`'s
#: shape exactly: `licenses/` holds this builder's own generated EULA
#: REFERENCE texts (not verbatim NVIDIA copyrighted text -- see the builder's
#: own doc comment), `notices/` this builder's own generated NOTICE.
CUDA_PACK_PATH_PREFIX_LICENSE: Final[tuple[tuple[str, str], ...]] = (
    ("licenses/", GENERATED_ARTIFACT_LICENSE),
    ("notices/", GENERATED_ARTIFACT_LICENSE),
)

#: `bin/` basename PREFIX rules (see this category's header for why a prefix,
#: not an exact table, is the evidence-backed shape here). Order does not
#: matter: `cublas*`/`nvblas*`/`cudnn*` never overlap, and the builder itself
#: refuses to flatten two wheels' files onto a colliding basename before this
#: classifier is ever consulted.
#:
#: ``nvblas`` covers ``nvblas64_12.dll``: the ``nvidia-cublas-cu12`` wheel's
#: own ``nvidia/cublas/bin/`` directory ships it alongside
#: ``cublas64_12.dll``/``cublasLt64_12.dll`` (NVIDIA's drop-in BLAS
#: replacement library, part of the same cuBLAS distribution under the same
#: CUDA Toolkit EULA) -- confirmed 2026-08-17 against the pinned wheel's own
#: contents after this omission made every native-cuda-runtime pack build
#: fail closed via ``_require_full_license_provenance``.
CUDA_PACK_BIN_BASENAME_PREFIX_LICENSE: Final[tuple[tuple[str, str], ...]] = (
    ("cublas", CUDA_TOOLKIT_EULA_LICENSE),
    ("nvblas", CUDA_TOOLKIT_EULA_LICENSE),
    ("cudnn", CUDNN_EULA_LICENSE),
)


def classify_cuda_pack_file(path: str) -> str | None:
    """Classify one `native-cuda-runtime` pack path (forward-slash, relative
    to the pack payload root) to its evidence-backed SPDX (or `LicenseRef-*`)
    license identifier.

    Same AC7 contract as `classify_ffmpeg_pack_file`/`classify_server_pack_
    file`: an unconfirmed path returns `None`, never a guessed default.
    Resolution order: directory prefix (`CUDA_PACK_PATH_PREFIX_LICENSE`),
    then a `bin/`-scoped basename PREFIX match
    (`CUDA_PACK_BIN_BASENAME_PREFIX_LICENSE`) -- basename-prefix matching is
    deliberately scoped to `bin/` only, so a path this category was never
    meant to classify (e.g. a stray top-level file) cannot accidentally match
    a DLL-naming heuristic.
    """
    for prefix, license_ in CUDA_PACK_PATH_PREFIX_LICENSE:
        if path.startswith(prefix):
            return license_

    if not path.startswith("bin/"):
        return None
    basename = path.rsplit("/", 1)[-1].casefold()
    for prefix, license_ in CUDA_PACK_BIN_BASENAME_PREFIX_LICENSE:
        if basename.startswith(prefix):
            return license_
    return None


# Defense-in-depth, at import time, exactly as Category 6/7 already do:
# neither NVIDIA license string above names GPL, so this is a zero-cost
# invariant today -- but it means a future edit that ever changed either
# constant to something GPL-family would fail at IMPORT, before any pack is
# built, rather than at review time or not at all.
for _cuda_pack_prefix, _cuda_pack_license in CUDA_PACK_BIN_BASENAME_PREFIX_LICENSE:
    if is_gpl_license(_cuda_pack_license):
        raise RuntimeError(
            "civiccast.native.runtime_licenses: GPL-family license recorded for "
            f"native-cuda-runtime pack prefix {_cuda_pack_prefix!r} "
            f"({_cuda_pack_license!r}) -- neither cuBLAS nor cuDNN is GPL-licensed; "
            "this indicates a corrupted constant, not a real license change"
        )
del _cuda_pack_prefix, _cuda_pack_license
del _ffmpeg_pack_basename, _ffmpeg_pack_license
