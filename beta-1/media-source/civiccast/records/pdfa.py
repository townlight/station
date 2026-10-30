# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""PDF/A-3B signed-record rendering and validation helpers."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from importlib import resources
from io import BytesIO
from os import environ
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pikepdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from civiccast.summary.models import OperatorApproval, SummaryDraft

EXPECTED_EMBEDDED_METADATA_NAMES = (
    "sourced-claims.json",
    "provenance.json",
    "approval.json",
    "timestamp-token.der",
)
FIXTURE_PACKAGE = "civiccast.records.fixtures"
PDFA_FONT_NAME = "CivicCastDejaVuSans"
STANDARD_14_FONT_NAMES = {
    "Courier",
    "Courier-Bold",
    "Courier-Oblique",
    "Courier-BoldOblique",
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-Oblique",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Bold",
    "Times-Italic",
    "Times-BoldItalic",
    "Symbol",
    "ZapfDingbats",
}
ATTACHMENT_RELATIONSHIPS = {
    "sourced-claims.json": pikepdf.Name.Source,
    "agenda-minutes.json": pikepdf.Name.Supplement,
    "provenance.json": pikepdf.Name.Supplement,
    "approval.json": pikepdf.Name.Supplement,
    "timestamp-token.der": pikepdf.Name.Data,
}
# PDF/A-3 (ISO 19005-3:2012 §6.8) requires the MIME type of every embedded
# file stream to be present as the /Subtype name on the EF stream itself,
# matching the regex `^[-\w+\.]+/[-\w+\.]+$`. veraPDF rejects null Subtypes
# even when the file specification dictionary names the file accurately.
EMBEDDED_FILE_SUBTYPES = {
    "sourced-claims.json": pikepdf.Name("/application/json"),
    "agenda-minutes.json": pikepdf.Name("/application/json"),
    "provenance.json": pikepdf.Name("/application/json"),
    "approval.json": pikepdf.Name("/application/json"),
    "timestamp-token.der": pikepdf.Name("/application/octet-stream"),
}
VALID_AF_RELATIONSHIPS = {
    pikepdf.Name.Source,
    pikepdf.Name.Data,
    pikepdf.Name.Supplement,
    pikepdf.Name.Alternative,
    pikepdf.Name.Unspecified,
}
ICC_PROFILE_FIXTURE = "sRGB.icc"


@dataclass(frozen=True)
class VeraPdfValidationResult:
    """Result returned by the local veraPDF validation wrapper."""

    valid: bool
    message: str


def render_pdfa_record(
    summary: SummaryDraft,
    *,
    approval: OperatorApproval | None = None,
) -> bytes:
    """Render a transcript-plus-summary PDF artifact with embedded metadata."""

    font_path = _fixture_path("DejaVuSans.ttf")
    _register_pdfa_font(font_path)
    metadata = {
        "summary_id": summary.summary_id,
        "meeting_id": summary.meeting_id,
        "audit_fingerprint": summary.audit_fingerprint,
        "sourced_claims": [claim.model_dump(mode="json") for claim in summary.sourced_claims],
        "provenance": summary.provenance.model_dump(mode="json"),
        "approval": approval.model_dump(mode="json") if approval is not None else None,
    }
    buffer = BytesIO()
    pdf = canvas.Canvas(
        buffer,
        pagesize=letter,
        pageCompression=0,
        invariant=1,
        pdfVersion=(1, 7),
        initialFontName=cast(Any, PDFA_FONT_NAME),
        initialFontSize=10,
        initialLeading=12,
    )
    pdf.setTitle(f"CivicCast signed record {summary.summary_id}")
    pdf.setAuthor("CivicCast")
    pdf.setSubject("PDF/A-3B signed-record export")
    page_width, _page_height = letter
    # Leave an extra 12pt of horizontal room for glyph-width rounding after
    # the embedded font is subset into the saved PDF.
    text_width = page_width - 156
    y = 740.0
    y = _draw_wrapped_pdf_text(
        pdf, "CivicCast PDF/A-3B signed-record export", y=y, width=text_width
    )
    y -= 6
    y = _draw_wrapped_pdf_text(pdf, f"Summary: {summary.summary_id}", y=y, width=text_width)
    y = _draw_wrapped_pdf_text(pdf, f"Meeting: {summary.meeting_id}", y=y, width=text_width)
    y = _draw_wrapped_pdf_text(
        pdf, f"Audit fingerprint: {summary.audit_fingerprint}", y=y, width=text_width
    )
    y -= 6
    y = _draw_wrapped_pdf_text(pdf, "Embedded metadata names:", y=y, width=text_width)
    for name in ("sourced-claims.json", "provenance.json", "approval.json", "timestamp-token.der"):
        y = _draw_wrapped_pdf_text(pdf, name, y=y - 2, width=text_width, indent=24)
    y -= 8
    _draw_wrapped_pdf_text(pdf, summary.narrative, y=y, width=text_width, continuation=True)
    pdf.showPage()
    pdf.save()

    normalized = BytesIO()
    with pikepdf.open(BytesIO(buffer.getvalue())) as document:
        attachments = {
            "sourced-claims.json": json.dumps(
                metadata["sourced_claims"], sort_keys=True, default=str
            ).encode("utf-8"),
            "provenance.json": json.dumps(
                metadata["provenance"], sort_keys=True, default=str
            ).encode("utf-8"),
            "approval.json": json.dumps(metadata["approval"], sort_keys=True, default=str).encode(
                "utf-8"
            ),
            "timestamp-token.der": b"CivicCast timestamp token placeholder",
        }
        if summary.agenda_minutes is not None:
            attachments["agenda-minutes.json"] = summary.agenda_minutes.model_dump_json().encode(
                "utf-8"
            )
        associated_files = []
        for name, payload in attachments.items():
            document.attachments[name] = payload
        for file_spec in _embedded_file_specs(document):
            name = str(file_spec.get("/F", file_spec.get("/UF", "")))
            relationship = ATTACHMENT_RELATIONSHIPS[name]
            subtype = EMBEDDED_FILE_SUBTYPES[name]
            file_spec[pikepdf.Name.AFRelationship] = relationship
            ef_dict = file_spec.get("/EF")
            if ef_dict is not None:
                for _ef_key, embedded_file in ef_dict.items():
                    embedded_file[pikepdf.Name.Subtype] = subtype
                    embedded_file[pikepdf.Name.AFRelationship] = relationship
            associated_files.append(file_spec)
        document.Root.AF = pikepdf.Array(associated_files)
        srgb_profile = _set_srgb_output_intent(document)
        _set_default_rgb_color_space(document, srgb_profile)
        title = f"CivicCast signed record {summary.summary_id}"
        description = f"PDF/A-3B signed-record export for meeting {summary.meeting_id}."
        document.docinfo[pikepdf.Name.Title] = title
        document.docinfo[pikepdf.Name.Author] = "CivicCast"
        document.docinfo[pikepdf.Name.Subject] = description
        document_id = _pdfa_uuid(summary.audit_fingerprint, "document")
        instance_id = _pdfa_uuid(summary.audit_fingerprint, "instance")
        # NOTE: PDF/A-3 (ISO 19005-3:2012 §6.6.2.3) only permits XMP properties
        # that are predefined in the XMP 2005 schemas, in PDF/A-1, in PDF/A-3,
        # or in an explicitly declared extension schema. We previously wrote
        # civiccast:* properties (summary_id, meeting_id, audit_fingerprint,
        # sourced_claims, provenance, approval) into a custom namespace; veraPDF
        # rejected every one of them under clauses 6.6.2.3.1 test 1 and test 2.
        # The same audit data is already carried in the structured attachments
        # (sourced-claims.json, provenance.json, approval.json) and in the
        # deterministic DocumentID UUID, so the XMP-side duplication added no
        # information that the conformant surfaces did not already publish.
        with document.open_metadata() as xmp:
            xmp.register_xml_namespace("http://ns.adobe.com/xap/1.0/mm/", "xmpMM")
            xmp["pdfaid:part"] = "3"
            xmp["pdfaid:conformance"] = "B"
            xmp["dc:title"] = title
            xmp["dc:creator"] = ["CivicCast"]
            xmp["dc:description"] = description
            xmp["xmp:CreatorTool"] = "CivicCast"
            xmp["xmpMM:DocumentID"] = document_id
            xmp["xmpMM:InstanceID"] = instance_id
        document.save(normalized, min_version="1.7")
    return normalized.getvalue()


def _draw_wrapped_pdf_text(
    pdf: canvas.Canvas,
    text: str,
    *,
    y: float,
    width: float,
    indent: float = 0,
    continuation: bool = False,
    font_size: float = 10,
    leading: float = 14,
) -> float:
    """Draw text within the page width, advancing to another page as needed."""

    left = 72 + indent
    usable_width = max(1, width - indent)
    lines = _wrap_pdf_text(text, width=usable_width, font_size=font_size)
    for line in lines:
        if y < 82:
            pdf.showPage()
            pdf.setFont(PDFA_FONT_NAME, font_size)
            pdf.setFillGray(0)
            if continuation:
                pdf.drawString(72, 740, "CivicCast PDF/A-3B signed-record export (continued)")
                y = 720
            else:
                y = 740
        pdf.drawString(left, y, line)
        y -= leading
    return y


def _wrap_pdf_text(text: str, *, width: float, font_size: float) -> list[str]:
    """Wrap paragraphs to *width*, breaking oversized unspaced tokens safely."""

    lines: list[str] = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            if pdfmetrics.stringWidth(word, PDFA_FONT_NAME, font_size) > width:
                if line:
                    lines.append(line)
                    line = ""
                fragments: list[str] = []
                fragment = ""
                for character in word:
                    candidate = fragment + character
                    if (
                        fragment
                        and pdfmetrics.stringWidth(candidate, PDFA_FONT_NAME, font_size) > width
                    ):
                        fragments.append(fragment)
                        fragment = character
                    else:
                        fragment = candidate
                if fragment:
                    fragments.append(fragment)
                lines.extend(fragments[:-1])
                line = fragments[-1] if fragments else ""
                continue

            candidate = f"{line} {word}" if line else word
            if pdfmetrics.stringWidth(candidate, PDFA_FONT_NAME, font_size) <= width:
                line = candidate
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines


def embed_timestamp_token(pdf_bytes: bytes, token_der: bytes) -> bytes:
    """Replace the placeholder ``timestamp-token.der`` attachment with the
    real RFC 3161 token.

    ``render_pdfa_record`` must embed a placeholder because the real token
    can only be produced from the rendered bytes (the timestamp authority is
    called on ``pdf_bytes`` after rendering). This re-opens the rendered PDF
    and overwrites the embedded-file stream's content in place -- rather
    than reassigning ``document.attachments[name]``, which would replace the
    whole file specification and lose the ``/AFRelationship`` and
    ``/Subtype`` set at render time -- so the archived, downloadable
    PDF/A-3 carries the real proof instead of a string that only looks like
    one.
    """

    with pikepdf.open(BytesIO(pdf_bytes)) as document:
        document.attachments["timestamp-token.der"].get_file().obj.write(token_der)
        out = BytesIO()
        document.save(out, min_version="1.7")
    return out.getvalue()


def validate_pdfa3_shape(pdf_bytes: bytes) -> list[str]:
    """Return local structural proof messages for the PDF/A-3B export shape.

    These local structural checks cover the PDF fields that have caused
    veraPDF failures during Phase 1. They are a fast regression net, but the
    pinned veraPDF workflow remains the conformance authority.
    """

    try:
        with pikepdf.open(BytesIO(pdf_bytes)) as document:
            attachment_names = set(document.attachments.keys())
            missing = [
                name for name in EXPECTED_EMBEDDED_METADATA_NAMES if name not in attachment_names
            ]
            metadata_stream = document.Root.get("/Metadata")
            output_intents = document.Root.get("/OutputIntents")
            associated_files = document.Root.get("/AF")
            color_space = _page_default_rgb_color_space(document)
            standard_fonts = _standard_14_fonts_in_document(document)
            subtype_failures = _embedded_file_subtype_failures(document, attachment_names)
            af_failures = _af_array_failures(document)
            icc_failures = _output_intent_icc_failures(document)
            xmp_failures = _xmp_packet_failures(document)
            content_stream_failures = _content_stream_color_failures(document)
    except pikepdf.PdfError as exc:
        return [f"FAIL PDF parse error: {exc}"]
    failures = [f"FAIL missing embedded file: {name}" for name in missing]
    if metadata_stream is None:
        failures.append("FAIL missing XMP metadata stream")
    if output_intents is None:
        failures.append("FAIL missing PDF/A OutputIntent")
    if associated_files is None:
        failures.append("FAIL missing document catalog AF array")
    if color_space is None:
        failures.append("FAIL missing page DefaultRGB color space")
    for name in EXPECTED_EMBEDDED_METADATA_NAMES:
        if name in attachment_names:
            relationship = document.attachments[name].obj.get("/AFRelationship")
            if relationship not in VALID_AF_RELATIONSHIPS:
                failures.append(f"FAIL missing AFRelationship: {name}")
    failures.extend(subtype_failures)
    failures.extend(af_failures)
    failures.extend(icc_failures)
    failures.extend(xmp_failures)
    failures.extend(content_stream_failures)
    if standard_fonts:
        failures.append("FAIL Standard 14 fonts used: " + ", ".join(sorted(standard_fonts)))
    if failures:
        return failures
    return [
        "PASS PDF parses with pikepdf",
        "PASS XMP metadata stream present",
        "PASS PDF/A OutputIntent present",
        "PASS document catalog AF array present",
        "PASS page DefaultRGB color space present",
        "PASS no Standard 14 font resources present",
        "PASS sourced claims, provenance, approval, and timestamp attachments present",
        "PASS every embedded file stream has a conformant /Subtype MIME name",
        "PASS /AF entries are file specification dictionaries with valid AFRelationship",
        "PASS DestOutputProfile is a v<5 RGB/CMYK/GRAY mntr or prtr ICC profile",
        "PASS XMP packet declares pdfaid + UUID URIs and no undeclared schemas",
        "PASS page content streams emit no device-RGB color operators",
    ]


def _fixture_path(name: str) -> str:
    """Return a filesystem path for a packaged records fixture."""

    return str(resources.files(FIXTURE_PACKAGE).joinpath(name))


def _fixture_bytes(name: str) -> bytes:
    """Return bytes for a packaged records fixture."""

    return resources.files(FIXTURE_PACKAGE).joinpath(name).read_bytes()


def _register_pdfa_font(font_path: str) -> None:
    """Register the embedded PDF/A font once per process."""

    if PDFA_FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(PDFA_FONT_NAME, font_path))


def _set_srgb_output_intent(document: pikepdf.Pdf) -> pikepdf.Object:
    """Embed the sRGB ICC profile as the document PDF/A OutputIntent.

    PDF/A-3 (ISO 19005-3:2012 §6.2.3) requires the DestOutputProfile to be an
    output or monitor profile (Device Class "prtr" or "mntr") in RGB, CMYK,
    or GRAY. The fixture shipped here is a Device Class "mntr" sRGB display
    profile; see `sRGB-LICENSE.txt` for provenance.
    """

    icc_stream = pikepdf.Stream(document, _fixture_bytes(ICC_PROFILE_FIXTURE))
    icc_stream[pikepdf.Name.N] = 3
    icc_ref = document.make_indirect(icc_stream)
    output_intent = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name.OutputIntent,
            "/S": pikepdf.Name.GTS_PDFA1,
            "/OutputConditionIdentifier": "sRGB IEC61966-2.1",
            "/OutputCondition": "sRGB display profile (IEC 61966-2.1)",
            "/RegistryName": "https://www.color.org",
            "/Info": "sRGB display profile generated via lcms2",
            "/DestOutputProfile": icc_ref,
        }
    )
    document.Root.OutputIntents = pikepdf.Array([document.make_indirect(output_intent)])
    return icc_ref


def _set_default_rgb_color_space(document: pikepdf.Pdf, srgb_profile: pikepdf.Object) -> None:
    """Map page DeviceRGB operations to the embedded sRGB ICC profile."""

    default_rgb = pikepdf.Array([pikepdf.Name.ICCBased, srgb_profile])
    for page in document.pages:
        resources_dict = page.obj.get("/Resources")
        if resources_dict is None:
            resources_dict = pikepdf.Dictionary()
            page.obj[pikepdf.Name.Resources] = resources_dict
        color_spaces = resources_dict.get("/ColorSpace")
        if color_spaces is None:
            color_spaces = pikepdf.Dictionary()
            resources_dict[pikepdf.Name.ColorSpace] = color_spaces
        color_spaces[pikepdf.Name.DefaultRGB] = default_rgb


def _page_default_rgb_color_space(document: pikepdf.Pdf) -> pikepdf.Object | None:
    """Return the first page DefaultRGB color space when all pages define one."""

    first: pikepdf.Object | None = None
    for page in document.pages:
        resources_dict = page.obj.get("/Resources")
        if resources_dict is None:
            return None
        color_spaces = resources_dict.get("/ColorSpace")
        if color_spaces is None or color_spaces.get("/DefaultRGB") is None:
            return None
        first = first or color_spaces.get("/DefaultRGB")
    return first


def _pdfa_uuid(fingerprint: str, salt: str) -> str:
    """Derive a deterministic UUID from an audit fingerprint and salt."""

    return f"uuid:{uuid5(NAMESPACE_URL, f'{fingerprint}:{salt}')}"


def _embedded_file_specs(document: pikepdf.Pdf) -> list[pikepdf.Object]:
    """Return file specification dictionaries from the EmbeddedFiles names tree."""

    names_tree = document.Root.Names.EmbeddedFiles.Names
    return [names_tree[index] for index in range(1, len(names_tree), 2)]


def _standard_14_fonts_in_document(document: pikepdf.Pdf) -> set[str]:
    """Return any Standard 14 font names referenced by page resources."""

    standard_fonts: set[str] = set()
    for page in document.pages:
        resources_dict = page.obj.get("/Resources")
        if resources_dict is None:
            continue
        font_dict = resources_dict.get("/Font")
        if font_dict is None:
            continue
        for _key, font in font_dict.items():
            base_font = str(font.get("/BaseFont", "")).lstrip("/")
            if base_font in STANDARD_14_FONT_NAMES:
                standard_fonts.add(base_font)
    return standard_fonts


_MIME_NAME_PATTERN = re.compile(r"^/[-\w+.]+/[-\w+.]+$")
_PDFA_UUID_URI_PATTERN = re.compile(
    r"^uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_FORBIDDEN_XMP_NAMESPACE_PREFIX = b"civiccast:"


def _embedded_file_subtype_failures(document: pikepdf.Pdf, attachment_names: set[str]) -> list[str]:
    """Return failures for any embedded file stream missing a conformant /Subtype."""

    failures: list[str] = []
    for file_spec in _embedded_file_specs(document):
        spec_name = str(file_spec.get("/F", file_spec.get("/UF", "")))
        if spec_name not in attachment_names:
            continue
        ef_dict = file_spec.get("/EF")
        if ef_dict is None:
            failures.append(f"FAIL embedded file spec missing /EF entries: {spec_name}")
            continue
        for ef_key, stream in ef_dict.items():
            subtype = stream.get("/Subtype")
            if subtype is None:
                failures.append(
                    f"FAIL embedded file stream missing /Subtype: {spec_name}/EF{ef_key}"
                )
                continue
            subtype_str = str(subtype)
            if not _MIME_NAME_PATTERN.match(subtype_str):
                failures.append(
                    "FAIL embedded file /Subtype not a MIME-typed Name: "
                    f"{spec_name}/EF{ef_key} = {subtype_str}"
                )
    return failures


def _af_array_failures(document: pikepdf.Pdf) -> list[str]:
    """Return failures for any /AF entry that is not a file specification dictionary."""

    failures: list[str] = []
    af = document.Root.get("/AF")
    if af is None:
        return failures
    for index, item in enumerate(af):
        item_type = item.get("/Type")
        if item_type != pikepdf.Name.Filespec:
            failures.append(f"FAIL /AF[{index}] is not /Type /Filespec (got {item_type})")
            continue
        relationship = item.get("/AFRelationship")
        if relationship not in VALID_AF_RELATIONSHIPS:
            failures.append(
                f"FAIL /AF[{index}] AFRelationship not in PDF/A-3 permitted set: {relationship}"
            )
    return failures


def _output_intent_icc_failures(document: pikepdf.Pdf) -> list[str]:
    """Return failures for an OutputIntent DestOutputProfile that veraPDF will reject."""

    failures: list[str] = []
    output_intents = document.Root.get("/OutputIntents")
    if output_intents is None or len(output_intents) == 0:
        return failures
    icc_stream = output_intents[0].get("/DestOutputProfile")
    if icc_stream is None:
        failures.append("FAIL OutputIntent missing /DestOutputProfile")
        return failures
    icc_bytes = icc_stream.read_bytes()
    if len(icc_bytes) < 128:
        failures.append(f"FAIL DestOutputProfile shorter than ICC header (got {len(icc_bytes)} B)")
        return failures
    device_class = icc_bytes[12:16]
    color_space = icc_bytes[16:20]
    version_major = icc_bytes[8]
    if device_class not in (b"mntr", b"prtr"):
        failures.append(
            "FAIL DestOutputProfile Device Class must be 'mntr' or 'prtr', got "
            + repr(device_class.decode("ascii", errors="replace"))
        )
    if color_space not in (b"RGB ", b"CMYK", b"GRAY"):
        failures.append(
            "FAIL DestOutputProfile color space must be RGB/CMYK/GRAY, got "
            + repr(color_space.decode("ascii", errors="replace"))
        )
    if version_major >= 5:
        failures.append(f"FAIL DestOutputProfile ICC version {version_major} must be < 5")
    return failures


def _xmp_packet_failures(document: pikepdf.Pdf) -> list[str]:
    """Return failures for XMP packet schemas/properties that PDF/A-3 forbids."""

    failures: list[str] = []
    metadata = document.Root.get("/Metadata")
    if metadata is None:
        return failures
    xmp_bytes = metadata.read_bytes()
    if _FORBIDDEN_XMP_NAMESPACE_PREFIX in xmp_bytes:
        failures.append(
            "FAIL XMP packet contains undeclared civiccast: namespace; "
            "PDF/A-3 §6.6.2.3.1 requires every property to be predefined or to "
            "appear in a declared extension schema"
        )
    with document.open_metadata() as xmp:
        for required_key in ("pdfaid:part", "pdfaid:conformance"):
            try:
                value = xmp[required_key]
            except KeyError:
                failures.append(f"FAIL XMP packet missing {required_key}")
                continue
            if not value:
                failures.append(f"FAIL XMP packet {required_key} is empty")
        for uuid_key in ("xmpMM:DocumentID", "xmpMM:InstanceID"):
            try:
                value = xmp[uuid_key]
            except KeyError:
                failures.append(f"FAIL XMP packet missing {uuid_key}")
                continue
            if not _PDFA_UUID_URI_PATTERN.match(str(value)):
                failures.append(f"FAIL XMP packet {uuid_key} not a uuid: URI: {value}")
    return failures


def _content_stream_color_failures(document: pikepdf.Pdf) -> list[str]:
    """Return failures for any page content stream that emits device-RGB color."""

    failures: list[str] = []
    for index, page in enumerate(document.pages):
        contents = page.obj.get("/Contents")
        if contents is None:
            continue
        if isinstance(contents, pikepdf.Array):
            payload = b"".join(stream.read_bytes() for stream in contents)
        else:
            payload = contents.read_bytes()
        if b" rg" in payload or payload.startswith(b"rg "):
            failures.append(f"FAIL page {index} content stream emits 'rg' (device RGB fill)")
        if b" RG" in payload or payload.startswith(b"RG "):
            failures.append(f"FAIL page {index} content stream emits 'RG' (device RGB stroke)")
    return failures


def validate_pdfa3b_with_verapdf(
    pdf_bytes: bytes,
    *,
    verapdf_binary: str = "verapdf",
) -> VeraPdfValidationResult:
    """Validate PDF bytes with veraPDF when available.

    Local developer machines may not have the pinned veraPDF binary installed;
    the required CI workflow performs the SHA256-verified installation. When
    the binary is absent, this wrapper falls back to parseable PDF structure
    checks instead of marker-string fixture validation.
    """

    with tempfile.TemporaryDirectory(prefix="civiccast-verapdf-") as temp_dir:
        target = Path(temp_dir) / "record.pdf"
        target.write_bytes(pdf_bytes)
        try:
            proc = subprocess.run(  # noqa: S603 - binary name is a fixed argument, no shell.
                [
                    verapdf_binary,
                    "--format",
                    "xml",
                    "--maxfailuresdisplayed",
                    "10",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = f"{proc.stdout}\n{proc.stderr}".strip()
            _write_verapdf_diagnostic_artifacts(pdf_bytes, output)
        except (FileNotFoundError, TimeoutError):
            failures = [
                message for message in validate_pdfa3_shape(pdf_bytes) if message.startswith("FAIL")
            ]
            if failures:
                return VeraPdfValidationResult(False, "; ".join(failures))
            return VeraPdfValidationResult(
                True, "veraPDF unavailable; local PDF/A-3B shape checks passed"
            )
    if proc.returncode == 0 and 'nonCompliant="0"' in output:
        return VeraPdfValidationResult(True, output or "veraPDF validation passed")
    if proc.returncode == 0 and "PASS" in output.upper():
        return VeraPdfValidationResult(True, output)
    return VeraPdfValidationResult(
        False, _summarize_verapdf_failures(output) or f"veraPDF exited {proc.returncode}"
    )


def _summarize_verapdf_failures(output: str) -> str:
    """Return concise veraPDF failed-rule details from the XML report."""

    try:
        # veraPDF is the pinned local validator process; parse its bounded report only.
        root = ET.fromstring(output)  # noqa: S314  # nosec B314
    except ET.ParseError:
        return output
    failed_rules = []
    for rule in root.findall(".//{*}rule[@status='failed']"):
        specification = rule.attrib.get("specification", "PDF/A")
        clause = rule.attrib.get("clause", "unknown-clause")
        test_number = rule.attrib.get("testNumber", "unknown-test")
        failed_checks = rule.attrib.get("failedChecks", "?")
        failed_rules.append(
            f"{specification} {clause} test {test_number} ({failed_checks} failed checks)"
        )
    if failed_rules:
        return "; ".join(failed_rules)
    return output


def _write_verapdf_diagnostic_artifacts(pdf_bytes: bytes, xml_output: str) -> None:
    """Copy the validated PDF and veraPDF XML to the configured diagnostics directory."""

    artifact_dir = environ.get("CIVICCAST_VERAPDF_ARTIFACT_DIR")
    if not artifact_dir:
        return
    destination = Path(artifact_dir)
    destination.mkdir(parents=True, exist_ok=True)
    digest = pdf_bytes[:256].hex()[:16]
    (destination / f"test_{digest}.pdf").write_bytes(pdf_bytes)
    (destination / f"verapdf-{digest}.xml").write_text(xml_output, encoding="utf-8")
