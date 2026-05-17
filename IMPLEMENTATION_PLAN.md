# DocuForge — Sprint 8 Implementation Plan

## Overview

Sprint 8 adds three capabilities to the processing pipeline:
1. **Document Classifier** — LLM-based document type detection
2. **Text Layer Verification** — Statistical bounding box validation before layering
3. **Structured Field Extraction** — Invoice date/amount/vendor extraction to JSON

---

## Phase 1: Document Classifier

### New / Modified Files

| File | Action | Purpose |
|------|--------|---------|
| `app/tagger.py` | Modify | Add `classify_document()` function |
| `app/processor.py` | Modify | Add `doc_type` field to `JobData`, call classifier in pipeline |
| `app/config.py` | Modify | Add `classify_model` setting |
| `app/main.py` | Modify | Add `GET /api/jobs/{id}/classify`, expose `doc_type` in job response |
| `app/templates/index.html` | Modify | Doc type badge in job history, type filter |

### Step 1.1: `classify_document()` in `app/tagger.py`

```python
# Classification prompt — returns single-word doc type
CLASSIFY_PROMPT = (
    "Classify this document as exactly one of: letter, invoice, form, "
    "quote, contract, report, other. Return ONLY the single word, "
    "no punctuation, no explanation.\n\nDocument text:\n{ocr_text}"
)

DOC_TYPES = frozenset({"letter", "invoice", "form", "quote", "contract", "report", "other"})

async def classify_document(ocr_text: str, settings: Settings) -> str:
    """
    Classify document type from first ~1000 chars of OCR text.

    Args:
        ocr_text: Raw OCR output text (will be truncated internally).
        settings: Application settings.

    Returns:
        One of: letter, invoice, form, quote, contract, report, other.
        Returns "other" on any failure.
    """
    if not ocr_text or not ocr_text.strip():
        return "other"

    truncated = ocr_text[:1000].strip()

    try:
        raw = await _call_ollama(
            CLASSIFY_PROMPT.format(ocr_text=truncated),
            settings,
        )
    except TaggingError:
        return "other"

    result = raw.strip().lower()
    return result if result in DOC_TYPES else "other"
```

**Design notes:**
- Reuses existing `_call_ollama()` — same Ollama codepath, zero new dependencies
- 1000-char truncation before prompt injection (not 4000 like rename/tags)
- `frozenset` lookup for fast validation of LLM output
- Fallback to `"other"` on any failure (no pipeline abort)

### Step 1.2: `JobData.doc_type` field

Add to `JobData.__init__` in `app/processor.py`:
```python
self.doc_type: Optional[str] = None
```

Include in `JobData.to_dict()`:
```python
result["doc_type"] = self.doc_type
```

### Step 1.3: Insert classification into pipeline

In `process_file()` in `app/processor.py`, after OCR loop (after step 3), before PDF/A conversion (before step 4):

```python
# ---- Step 3.5: Classify document type ----
doc_type = "other"
if ocr_full_text and settings.tagging_model:
    try:
        doc_type = await classify_document(ocr_full_text, settings)
        log_event("info", f"Document classified: {doc_type}", job_id)
    except Exception as exc:
        logger.warning(f"Classification failed (non-blocking): {exc}")
        doc_type = "other"

job_manager.update_job(job_id, doc_type=doc_type)
```

### Step 1.4: Config fields

Add to `Settings` in `app/config.py`:
```python
classify_model: str = Field(default="llama3.2", description="Ollama model for document classification")
rename_prompt_invoice: Optional[str] = Field(default=None, description="Type-specific rename prompt for invoices")
rename_prompt_contract: Optional[str] = Field(default=None, description="Type-specific rename prompt for contracts")
```

### Step 1.5: API endpoint

New route in `app/main.py`:
```python
@app.get("/api/jobs/{job_id}/classify")
async def get_classify(job_id: str, regenerate: bool = False):
    # If regenerate=True, re-run classifier and update job
    # Otherwise return stored doc_type
    pass
```

### Step 1.6: UI — Doc type badge

In `renderJobHistory()` add a colored badge cell:
```javascript
// doc type badge colors
const typeColors = {
    invoice: '#f59e0b',   // amber
    contract: '#3b82f6',  // blue
    letter: '#10b981',    // green
    form: '#8b5cf6',      // purple
    quote: '#06b6d4',     // cyan
    report: '#f97316',    // orange
    other: '#6b7280',      // gray
};
```

---

## Phase 2: Text Layer Verification

### New / Modified Files

| File | Action | Purpose |
|------|--------|---------|
| `app/verifier.py` | **Create** | Statistical bounding box validation |
| `app/processor.py` | Modify | Add `text_layer_score`, `text_layer_warnings` to JobData, gate layering |
| `app/config.py` | Modify | Add `verify_text_layer`, `verify_min_score` settings |
| `app/main.py` | Modify | Expose verification score in job responses |
| `app/templates/index.html` | Modify | Verification score indicator in job row |

### Step 2.1: `app/verifier.py`

```python
"""
DocuForge — Text Layer Quality Verifier
========================================
Statistical validation of OCR bounding boxes before they are
layered onto the PDF/A. Detects catastrophic alignment failures
without re-rendering or re-OCR.

Checks are fast CPU-only operations on the bounding box data.
A score 0-100 gates whether text is applied.
"""

def validate_text_layer(
    ocr_results: list[dict],
    dpi: int,
    page_dimensions: list[tuple[float, float]],
) -> dict:
    """
    Run statistical checks on OCR bounding boxes.

    Args:
        ocr_results: List of OCR result dicts, one per page.
        dpi: Rendering DPI used for OCR.
        page_dimensions: List of (width_pt, height_pt) per page.

    Returns:
        dict: {valid: bool, score: int, warnings: list[str], page_scores: list[int]}
    """


def _check_bounds(words: list[dict], page_w: float, page_h: float, dpi: int) -> list[str]:
    """Verify all bboxes fall within page pixel dimensions."""
    pass


def _check_position_diversity(words: list[dict]) -> list[str]:
    """At least N unique (x,y) positions — catches hallucination."""
    pass


def _check_area_sanity(words: list[dict], page_w: float, page_h: float) -> list[str]:
    """No zero-area or >90% page area bboxes."""
    pass


def _check_coverage(words: list[dict], page_w: float, page_h: float) -> list[str]:
    """Text layer covers 1-95% of page area."""
    pass


def _check_font_sizes(words: list[dict], scale: float) -> list[str]:
    """Computed font sizes 3-72 pt."""
    pass
```

**Scoring algorithm:**
```
score starts at 100
-10 per page with any bounds violation
-15 per page with zero position diversity (<3 unique positions)
-5 per page with area sanity failures
-5 per page with coverage <1% or >95%
-5 per page with font size outside 3-72pt
Floor at 0
```

**When verification fires:**
- Only for pages with `tier == "word"` and `words` list non-empty
- Pages with `tier in ("line", "full_page")` are excluded from verification (no bboxes to check)
- Pages with `tier == "skip"` are already marked failed and excluded

### Step 2.2: JobData fields

```python
# In JobData.__init__
self.text_layer_score: Optional[int] = None
self.text_layer_warnings: list[str] = []
```

### Step 2.3: Gating in pipeline

In `process_file()`, after OCR and before `layer_text_on_pdf()`:

```python
# ---- Step 4.5: Verify text layer quality ----
verify_result = {"valid": True, "score": 100, "warnings": [], "page_scores": []}
if settings.verify_text_layer:
    try:
        verify_result = validate_text_layer(ocr_results, settings.dpi, page_dims)
        job_manager.update_job(job_id,
            text_layer_score=verify_result["score"],
            text_layer_warnings=verify_result["warnings"],
        )
        if verify_result["score"] < settings.verify_min_score:
            log_event("warn", f"Text layer skipped: score {verify_result['score']} < {settings.verify_min_score}", job_id)
        elif verify_result["warnings"]:
            log_event("warn", f"Text layer applied with warnings: score {verify_result['score']}", job_id)
    except Exception as exc:
        logger.warning(f"Verification check failed (non-blocking): {exc}")
        verify_result["valid"] = True  # Proceed if verification itself crashes
```

Then gate `layer_text_on_pdf()`:
```python
if verify_result["valid"] and verify_result["score"] >= settings.verify_min_score:
    layer_text_on_pdf(pdfa_tmp, ocr_results, output_path, settings.dpi)
```

### Step 2.4: Config fields

```python
verify_text_layer: bool = Field(default=True)
verify_min_score: int = Field(default=50, ge=0, le=100)
```

---

## Phase 3: Structured Field Extraction

### New / Modified Files

| File | Action | Purpose |
|------|--------|---------|
| `app/tagger.py` | Modify | Add `extract_invoice_fields()` |
| `app/processor.py` | Modify | Add `extracted_fields` to JobData, write .json, call extractor |
| `app/config.py` | Modify | Add `extract_fields`, `extract_model` |
| `app/main.py` | Modify | Add `GET /api/jobs/{id}/fields` |
| `app/templates/index.html` | Modify | "Extracted Data" section in preview modal |

### Step 3.1: `extract_invoice_fields()` in `app/tagger.py`

```python
EXTRACT_FIELDS_PROMPT = (
    "Extract the following fields from this invoice text. "
    "Return ONLY valid JSON:\n"
    '{\n'
    '  "invoice_date": "YYYY-MM-DD or empty string if not found",\n'
    '  "total_amount": "numeric amount with currency symbol if present, or empty string",\n'
    '  "vendor_name": "company or individual name, or empty string"\n'
    '}\n\n'
    "Invoice text:\n{ocr_text}"
)

async def extract_invoice_fields(ocr_text: str, settings: Settings) -> dict:
    """
    Extract structured fields from invoice OCR text via LLM.

    Args:
        ocr_text: Full OCR text from the invoice document.
        settings: Application settings.

    Returns:
        dict with keys: invoice_date, total_amount, vendor_name.
        Values are strings; empty string if field not found.
        Returns empty dict on any failure.
    """
    if not ocr_text or not ocr_text.strip():
        return {}

    truncated = ocr_text[:MAX_OCR_TEXT_LENGTH].strip()

    try:
        raw = await _call_ollama(
            EXTRACT_FIELDS_PROMPT.format(ocr_text=truncated),
            settings,
        )
    except TaggingError:
        return {}

    return _parse_fields(raw)


def _parse_fields(raw: str) -> dict:
    """Parse structured field extraction JSON with defensive fallback."""
    try:
        data = json.loads(raw)
        return {
            "invoice_date": str(data.get("invoice_date", "")).strip(),
            "total_amount": str(data.get("total_amount", "")).strip(),
            "vendor_name": str(data.get("vendor_name", "")).strip(),
        }
    except json.JSONDecodeError:
        pass
    # Tier 2: strip markdown
    cleaned = raw.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    try:
        data = json.loads(cleaned)
        return {
            "invoice_date": str(data.get("invoice_date", "")).strip(),
            "total_amount": str(data.get("total_amount", "")).strip(),
            "vendor_name": str(data.get("vendor_name", "")).strip(),
        }
    except json.JSONDecodeError:
        pass
    logger.warning(f"Could not parse field extraction response: {raw[:200]}")
    return {}
```

### Step 3.2: JobData field + JSON file

```python
# In JobData.__init__
self.extracted_fields: Optional[dict] = None
```

In `process_file()`, after classification, only when `doc_type == "invoice"`:

```python
# ---- Step 3.6: Extract structured fields (invoices only) ----
extracted_fields = {}
if (settings.extract_fields
    and doc_type == "invoice"
    and ocr_full_text
    and settings.tagging_model):
    try:
        extracted_fields = await extract_invoice_fields(ocr_full_text, settings)
        log_event("info", f"Fields extracted: {json.dumps(extracted_fields)}", job_id)
    except Exception as exc:
        logger.warning(f"Field extraction failed (non-blocking): {exc}")
        extracted_fields = {}

# Write .json file alongside output PDF
if extracted_fields:
    fields_path = output_path.with_suffix(".json")
    with open(fields_path, "w") as f:
        json.dump(extracted_fields, f, indent=2)

job_manager.update_job(job_id, extracted_fields=extracted_fields)
```

### Step 3.3: Config fields

```python
extract_fields: bool = Field(default=True)
extract_model: str = Field(default="llama3.2")
```

### Step 3.4: API endpoint

```python
@app.get("/api/jobs/{job_id}/fields")
async def get_fields(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404)
    return JSONResponse({
        "doc_type": job.doc_type,
        "fields": job.extracted_fields or {},
    })
```

### Step 3.5: UI — Extracted Data section

In the preview/approve modal, add a collapsible section below proposed name/tags:

```html
<div class="extracted-fields" id="extracted-fields-section"
     style="display: none;">
    <h4>Extracted Data</h4>
    <div class="field-row">
        <label>Invoice Date</label>
        <input type="text" id="extracted-date"
               placeholder="YYYY-MM-DD" />
    </div>
    <div class="field-row">
        <label>Total Amount</label>
        <input type="text" id="extracted-amount"
               placeholder="$0.00" />
    </div>
    <div class="field-row">
        <label>Vendor</label>
        <input type="text" id="extracted-vendor"
               placeholder="Company name" />
    </div>
</div>
```

Show section when `job.doc_type === "invoice"` and `job.extracted_fields` exists.

---

## Full Pipeline Order (After Sprint 8)

```
1. Upload file
2. If image → convert to PDF (img2pdf)
3. Render pages to PNG (pdf2image)
4. OCR each page (Tesseract or GLM-OCR)
5. DOCUMENT CLASSIFICATION (LLM call, ~1s)
6. TEXT LAYER VERIFICATION (stats only, ~1ms)
   └─ Score < 50: skip text layer, warn
7. Convert to PDF/A (pikepdf)
8. Layer OCR text (pikepdf) — gated by verification score
9. STRUCTURED FIELD EXTRACTION — only if doc_type=invoice
   └─ Write .json alongside output
10. LLM Rename + Tags (with type-specific prompts)
11. Embed metadata (tags, title, fields) in PDF/A XMP
12. Save output / await approval
```

---

## Testing Strategy

### Phase 1 (Classifier)
- Upload 1 invoice PDF → verify `doc_type == "invoice"` in API response
- Upload 1 contract PDF → verify `doc_type == "contract"`
- Upload a blank page → verify `doc_type == "other"`
- Kill Ollama → upload anything → verify classifier returns `"other"` without blocking pipeline

### Phase 2 (Verification)
- Valid OCR results → score ≥ 80, text layer applied
- Feed hallucinated bboxes (all same coordinates) → score < 50, text layer skipped
- Feed bboxes with 0 area → warning logged
- Feed page with no word-tier results → verification skipped (no bboxes to check)

### Phase 3 (Extraction)
- Invoice PDF → fields populated, .json created
- Non-invoice PDF → extraction never fires
- Kill Ollama during extraction → pipeline continues, fields empty
- Re-run extraction with `regenerate=true` param

---

## Rollout Plan

1. **Phase 1 first** — classifier has zero risk to existing pipeline
2. **Phase 2 in parallel** — verification is independent, can deploy same day
3. **Phase 3 after Phase 1 validated** — depends on `doc_type` being correct

Each phase deploys as a single commit after verification testing.
