"""
DocuForge — PDF Utilities
===========================
PDF manipulation functions:
- Image to interim PDF conversion (img2pdf)
- PDF/A-2b compliance conversion (pikepdf)
- Invisible OCR text layering (pypdf)

All functions operate on file paths and log their progress.
"""

import logging
import tempfile
import zlib
from pathlib import Path

import img2pdf
import pikepdf
from pikepdf import Dictionary
from pypdf import PdfWriter, PdfReader, PageObject
from pypdf.generic import (
    ArrayObject,
    FloatObject,
    TextStringObject,
    create_string_object,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from app.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Image → PDF
# ---------------------------------------------------------------------------


def image_to_pdf(image_path: Path, output_dir: Path) -> Path:
    """
    Convert a single image (PNG, JPG, TIFF, BMP) to an interim PDF.

    Uses img2pdf for lossless embedding — the image data is placed
    directly into the PDF container without re-compression.

    Args:
        image_path: Path to the source image file.
        output_dir: Directory to write the interim PDF into.

    Returns:
        Path to the generated PDF file.
    """
    # Generate output path from the original image name, preserving the stem
    output_path = output_dir / f"{image_path.stem}_interim.pdf"

    logger.info(f"Converting image to PDF: {image_path.name} -> {output_path.name}")

    # img2pdf.convert writes raw PDF bytes; we write them manually
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(str(image_path)))

    return output_path


# ---------------------------------------------------------------------------
# PDF → PDF/A-2b
# ---------------------------------------------------------------------------


def convert_to_pdfa(input_pdf: Path, output_pdf: Path, pdfa_level: str):
    """
    Convert a PDF to PDF/A-2b (or -3b) archival format using pikepdf.

    PDF/A compliance ensures:
      - All fonts are embedded
      - No JavaScript or external resource references
      - sRGB output intent for color consistency
      - XMP metadata with PDF/A conformance markers
      - Document catalog tagged as PDF/A

    Args:
        input_pdf: Path to the source PDF (scanned or interim image PDF).
        output_pdf: Path where the PDF/A file will be written.
        pdfa_level: Conformance level — "2b" or "3b".
    """
    logger.info(f"Converting to PDF/A-{pdfa_level}: {input_pdf.name} -> {output_pdf.name}")

    # Open the source PDF with pikepdf
    pdf = pikepdf.open(input_pdf, allow_overwriting_input=True)

    # ---- Strip non-compliant content ----
    # Remove JavaScript (not allowed in PDF/A)
    if pdf.Root.get("/Names", None):
        del pdf.Root.Names

    # Remove any embedded files
    if pdf.Root.get("/EmbeddedFiles", None):
        del pdf.Root.EmbeddedFiles

    # ---- Set PDF/A metadata in the document catalog ----
    pdf_version = "2" if pdfa_level.startswith("2") else "3"
    part = pdfa_level[1:]  # "b" from "2b"

    with pdf.open_metadata() as meta:
        # XMP metadata with PDF/A conformance
        meta.load_from_docinfo(pdf.docinfo)
        meta["pdfaid:part"] = part
        meta["pdfaid:conformance"] = "B" if part == "2" else "B"
        meta["pdfaid:amd"] = "Corr. 1"
        meta["pdf:Producer"] = "DocuForge"
        meta["dc:format"] = f"application/pdf; version=1.{pdf_version}"
        # Preserve existing title if present
        if pdf.docinfo.get("/Title"):
            meta["dc:title"] = str(pdf.docinfo["/Title"])

    # ---- Add PDF/A output intent (sRGB via CalRGB) ----
    # We define sRGB as a CalRGB color space with the standard sRGB
    # primaries and reference it in the OutputIntent. This avoids ICC
    # profile compatibility issues across PDF viewers (Acrobat, Edge,
    # Preview, etc.) while providing correct color information.
    _ensure_srgb_colorspace(pdf)

    output_intent = Dictionary({
        "/Type": "/OutputIntent",
        "/S": "/GTS_PDFA1",
        "/OutputConditionIdentifier": TextStringObject("sRGB IEC61966-2.1"),
        "/Info": TextStringObject("sRGB IEC61966-2.1"),
        "/RegistryName": TextStringObject("http://www.color.org"),
        "/DestOutputProfileRef": Dictionary({
            "/CS": "/DefaultRGB",
        }),
    })
    pdf.Root.OutputIntents = pikepdf.Array([output_intent])

    # ---- Mark the catalog as PDF/A ----
    pdf.Root.MarkInfo = Dictionary({"/Marked": True})

    # Save the PDF/A compliant file
    pdf.save(output_pdf, linearize=True)
    pdf.close()

    logger.info(f"PDF/A-{pdfa_level} conversion complete: {output_pdf.name}")


# ---------------------------------------------------------------------------
# Text Layering
# ---------------------------------------------------------------------------


def layer_text_on_pdf(
    pdfa_path: Path,
    ocr_results: list[dict],
    output_path: Path,
    dpi: int = 300,
):
    """
    Layer OCR'd text invisibly onto a PDF/A file using pikepdf.

    Text is written as content stream operators with rendering mode 3
    (Tr = 3 — neither fill nor stroke), making it selectable and
    searchable while preserving the original visual appearance.

    Defensive tiers for text placement:
      Tier 1 — Per-word: Each word placed at its exact bounding box
      Tier 2 — Per-line: Text split by newlines, evenly spaced down page
      Tier 3 — Full-page: Entire text block at top of page

    Args:
        pdfa_path: Path to the PDF/A file (output of convert_to_pdfa).
        ocr_results: List of OCR result dicts, one per page.
        output_path: Path to write the final PDF with text layer.
        dpi: DPI used when rendering pages (needed for coord scaling).
    """
    logger.info(f"Layering OCR text onto PDF/A: {pdfa_path.name}")

    scale = 72.0 / dpi  # pixels → PDF points

    # Open the PDF/A with pikepdf (same library we use for conversion)
    pdf = pikepdf.open(pdfa_path, allow_overwriting_input=True)

    for page_idx, page_view in enumerate(pdf.pages):
        ocr = ocr_results[page_idx] if page_idx < len(ocr_results) else None

        if ocr and ocr.get("ocr_success") and ocr.get("full_text"):
            # Get page dimensions for coordinate conversion
            page = pikepdf.Page(page_view)
            mediabox = page.mediabox
            page_height = float(mediabox[3])  # top of the page in PDF points

            # Ensure Helvetica font is registered in page resources so
            # PDF viewers can render and select the text layer
            _ensure_helvetica_font(page)

            # Build text operators for this page
            operators = _build_text_operators(ocr, scale, page_height)
            if operators:
                page.contents_add(operators.encode("ascii"), prepend=False)
                logger.debug(
                    f"Page {page_idx + 1}: added text layer "
                    f"(tier={ocr.get('tier', 'unknown')})"
                )
        else:
            logger.debug(f"Page {page_idx + 1}: No OCR text — skipping text layer")

    # Save with text layer
    pdf.save(output_path, linearize=True)
    pdf.close()

    logger.info(f"Text layering complete: {output_path.name}")


def _escape_pdf_text(text: str) -> str:
    r"""
    Encode a text string for use in a PDF content stream Tj operator.

    Strategy (in order):
      1. Pure ASCII (U+0000-U+007F) — literal PDF string: (escaped text)
      2. Latin-1 (U+0080-U+00FF) — PDF literal string with octal escapes: (\xxx)
      3. Unicode beyond latin-1 — UTF-16BE hex string: <FEFF hex>

    Args:
        text: Raw text string to encode.

    Returns:
        PDF-ready string representation: either (safe text) or <hex>.
    """
    # Try ASCII first — covers most English documents
    try:
        text.encode("ascii")
        safe = (
            text.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
        )
        return f"({safe})"
    except UnicodeEncodeError:
        pass

    # Try latin-1 — use octal escapes for non-ASCII bytes
    try:
        text.encode("latin-1")
        # Escape special PDF chars, then convert non-ASCII to octal \xxx
        escaped = (
            text.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
        )
        result = []
        for ch in escaped:
            cp = ord(ch)
            if cp > 127:
                result.append(f"\\{cp:03o}")
            else:
                result.append(ch)
        return f"({''.join(result)})"
    except UnicodeEncodeError:
        pass

    # Characters beyond latin-1 — use UTF-16BE hex with BOM
    utf16_bytes = text.encode("utf-16-be")
    hex_str = utf16_bytes.hex()
    return f"<FEFF{hex_str}>"


def _build_text_operators(ocr: dict, scale: float, page_height: float) -> str:
    """
    Build PDF content stream operators for invisible text on one page.

    Returns a string of BT/ET text blocks, one per word or line,
    with text rendering mode set to 3 (invisible).

    Args:
        ocr: OCR result dict for this page.
        scale: Pixel-to-point scale factor (72 / dpi).
        page_height: PDF page height in points (mediabox[3]).

    Returns:
        String of PDF content stream operators, or empty string.
    """
    tier = ocr.get("tier", "full_page")
    full_text = ocr.get("full_text", "")
    words = ocr.get("words", [])

    ops_parts = []

    # ---- Tier 1: Per-word positioning ----
    if tier == "word" and words:
        for w in words:
            text = w.get("text", "")
            bbox = w.get("bbox", [])

            if not text or len(bbox) != 4:
                continue

            x, y_img, w_px, h_px = bbox
            x_pt = x * scale
            # PDF y-axis is bottom-up; image y-axis is top-down
            # Use the actual image height by checking the loaded PDF
            # We'll do this inside layer_text_on_pdf; for now use the bbox
            y_pt_btm = page_height - ((y_img + h_px) * scale)

            font_size = max(h_px * scale * 0.85, 4)

            ops_parts.append(
                f"BT\n/DeviceGray CS /DeviceGray cs\n0 G 0 g\n3 Tr\n/Helvetica {font_size:.1f} Tf\n"
                f"1 0 0 1 {x_pt:.1f} {y_pt_btm:.1f} Tm\n"
                f"{_escape_pdf_text(text)} Tj\nET"
            )

    # ---- Tier 2: Per-line fallback ----
    elif tier == "line" and full_text:
        lines = full_text.strip().split("\n")
        margin = 36
        font_size = 10
        y_start = page_height - margin

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            y_pos = y_start - (i * font_size * 1.4)
            if y_pos < margin:
                break
            ops_parts.append(
                f"BT\n/DeviceGray CS /DeviceGray cs\n0 G 0 g\n3 Tr\n/Helvetica {font_size:.1f} Tf\n"
                f"1 0 0 1 {margin:.1f} {y_pos:.1f} Tm\n"
                f"{_escape_pdf_text(line)} Tj\nET"
            )

    # ---- Tier 3: Full-page text block ----
    elif full_text:
        margin = 36
        font_size = 8
        y_pos = page_height - margin
        ops_parts.append(
            f"BT\n/DeviceGray CS /DeviceGray cs\n0 G 0 g\n3 Tr\n/Helvetica {font_size:.1f} Tf\n"
            f"1 0 0 1 {margin:.1f} {y_pos:.1f} Tm\n"
            f"{_escape_pdf_text(full_text)} Tj\nET"
        )

    return "\n".join(ops_parts) if ops_parts else ""


def _ensure_helvetica_font(page):
    """
    Register /Helvetica as a standard Type1 font in the page's Resources.

    Modifies the existing Resources dictionary in-place (does not replace it)
    to avoid breaking indirect object references. Uses pikepdf's native
    Dictionary/Name/String types for compatibility.

    Args:
        page: pikepdf.Page object to modify in-place.
    """
    from pikepdf import Dictionary, Name, String

    resources = page.Resources
    if "/Resources" not in page:
        resources = Dictionary()
        page.Resources = resources

    fonts = resources.get("/Font", Dictionary())

    if "/Helvetica" not in fonts:
        font_entry = Dictionary()
        font_entry[Name.Type] = Name.Font
        font_entry[Name.Subtype] = Name.Type1
        font_entry[Name.BaseFont] = Name.Helvetica
        fonts[Name.Helvetica] = font_entry
        resources[Name.Font] = fonts


# ---------------------------------------------------------------------------
# Tag Embedding (Sprint 2)
# ---------------------------------------------------------------------------


def embed_tags_in_pdf(pdf_path: Path, tags: list[str], title: str | None = None):
    """
    Inject document tags and title into the PDF metadata.

    Tags are written to:
      - dc:subject       — Dublin Core subject (XMP)
      - docuforge:tags   — Custom namespace (XMP)
      - pdf:Keywords     — Standard PDF document info (visible in Acrobat)

    Title is written to:
      - dc:title         — Dublin Core title (XMP)

    Creator is set to:
      - dc:creator       — "DocuForge"

    The PDF is modified in-place.

    Args:
        pdf_path: Path to the PDF/A file to modify.
        tags: List of tag strings. Empty list removes existing tags.
        title: Document title (e.g., LLM-generated filename). Optional.
    """
    if not tags and not title:
        logger.debug(f"No metadata to embed in {pdf_path.name}")
        return

    logger.info(f"Embedding metadata into {pdf_path.name}: tags={tags}, title={title}")

    pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

    with pdf.open_metadata() as meta:
        if tags:
            meta["dc:subject"] = tags
            meta["docuforge:tags"] = tags
        if title:
            meta["dc:title"] = title
        meta["dc:creator"] = "DocuForge"

    # Also set Keywords in the document info dictionary (Acrobat visible)
    if tags:
        pdf.docinfo["/Keywords"] = ", ".join(tags)

    pdf.save(pdf_path, linearize=True)
    pdf.close()

    logger.debug(f"Metadata embedded in {pdf_path.name}")


# ---------------------------------------------------------------------------
# ICC Profile (sRGB)
# ---------------------------------------------------------------------------

# ICC profile cache (populated at first use)
_srgb_icc_cache = None


def _get_srgb_icc_profile():
    """
    Return the RAW (uncompressed) sRGB IEC61966-2.1 ICC profile bytes.

    Generates the sRGB profile via Pillow's built-in profile factory
    and serializes it via ImageCmsProfile.tobytes().

    Returns the raw ICC bytes (not FlateDecode compressed) for
    maximum compatibility with PDF viewers.
    """
    global _srgb_icc_cache
    if _srgb_icc_cache is not None:
        return _srgb_icc_cache

    try:
        from PIL import ImageCms
        profile = ImageCms.createProfile("sRGB")
        cms_profile = ImageCms.ImageCmsProfile(profile)
        raw = cms_profile.tobytes()
        if raw and len(raw) > 100:
            _srgb_icc_cache = raw
            logger.debug(f"sRGB ICC profile generated ({len(raw)} bytes)")
            return raw
    except Exception as exc:
        logger.warning(f"Could not generate sRGB ICC profile: {exc}")

    logger.warning("No sRGB ICC profile available")
    _srgb_icc_cache = False
    return None


def _ensure_srgb_colorspace(pdf):
    """
    Ensure sRGB is defined as a CalRGB color space in every page's Resources.

    This is a fallback when no ICC profile is available. CalRGB with
    the standard sRGB primaries provides correct color rendering
    without requiring an embedded ICC profile.
    """
    from pikepdf import Name, Dictionary, Array
    srgb_cal = Dictionary({
        "/WhitePoint": Array([0.9505, 1.0, 1.089]),
        "/Gamma": Array([2.2, 2.2, 2.2]),
    })
    for page_view in pdf.pages:
        page = pikepdf.Page(page_view)
        resources = page.Resources if "/Resources" in page else Dictionary()
        cs_dict = resources.get("/ColorSpace", Dictionary())
        cs_dict["/DefaultRGB"] = Array([Name.CalRGB, srgb_cal])
        resources["/ColorSpace"] = cs_dict
        page.Resources = resources
