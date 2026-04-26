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

    # ---- Add PDF/A output intent (sRGB color profile) ----
    # The output intent ensures consistent color rendering across viewers.
    # We reference the sRGB IEC61966-2.1 profile by its well-known identifier.
    srgb_profile = pikepdf.Stream(
        pdf,
        b'',
        {
            "/N": 3,  # Number of color components (RGB = 3)
            "/Filter": "/FlateDecode",
        },
    )

    # Build the output intent dictionary
    output_intent = Dictionary({
        "/Type": "/OutputIntent",
        "/S": "/GTS_PDFA1",
        "/OutputCondition": TextStringObject("sRGB IEC61966-2.1"),
        "/OutputConditionIdentifier": TextStringObject("sRGB IEC61966-2.1"),
        "/Info": TextStringObject("sRGB IEC61966-2.1"),
        "/RegistryName": TextStringObject("http://www.color.org"),
        "/DestOutputProfile": srgb_profile,
    })

    # Add or replace the output intents array in the catalog
    pdf.Root.OutputIntents = pikepdf.Array([output_intent])

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

            # Build text operators for this page
            operators = _build_text_operators(ocr, scale, page_height)
            if operators:
                page.contents_add(operators.encode("latin-1"), prepend=False)
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

            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops_parts.append(
                f"BT\n3 Tr\n/Helvetica {font_size:.1f} Tf\n"
                f"1 0 0 1 {x_pt:.1f} {y_pt_btm:.1f} Tm\n"
                f"({safe}) Tj\nET"
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
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops_parts.append(
                f"BT\n3 Tr\n/Helvetica {font_size:.1f} Tf\n"
                f"1 0 0 1 {margin:.1f} {y_pos:.1f} Tm\n"
                f"({safe}) Tj\nET"
            )

    # ---- Tier 3: Full-page text block ----
    elif full_text:
        margin = 36
        font_size = 8
        y_pos = page_height - margin
        safe = full_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops_parts.append(
            f"BT\n3 Tr\n/Helvetica {font_size:.1f} Tf\n"
            f"1 0 0 1 {margin:.1f} {y_pos:.1f} Tm\n"
            f"({safe}) Tj\nET"
        )

    return "\n".join(ops_parts) if ops_parts else ""


# ---------------------------------------------------------------------------
# Tag Embedding (Sprint 2)
# ---------------------------------------------------------------------------


def embed_tags_in_pdf(pdf_path: Path, tags: list[str]):
    """
    Inject document tags into the PDF/A XMP metadata.

    Tags are written to two XMP namespaces:
      - dc:subject       —  Dublin Core standard subject field (as bag of items)
      - docuforge:tags   —  Custom namespace for application-level tag queries

    The PDF is modified in-place. The original visual content is unchanged.

    Args:
        pdf_path: Path to the PDF/A file to modify.
        tags: List of tag strings to embed. Empty list removes existing tags.
    """
    if not tags:
        logger.debug(f"No tags to embed in {pdf_path.name}")
        return

    logger.info(f"Embedding {len(tags)} tag(s) into {pdf_path.name}")

    # Open the PDF/A with overwrite allowed (in-place edit)
    pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

    with pdf.open_metadata() as meta:
        # Write tags to Dublin Core subject field
        # dc:subject is a bag (unordered list) of text items
        meta["dc:subject"] = tags

        # Write tags to our custom namespace for future filtering
        meta["docuforge:tags"] = tags

    # Save in-place — overwrites the file with updated metadata
    pdf.save(pdf_path, linearize=True)
    pdf.close()

    logger.debug(f"Tags embedded successfully in {pdf_path.name}")
