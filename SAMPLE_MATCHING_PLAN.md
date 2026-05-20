# Sample-Guided Split Detection — Implementation Plan

## Goal

Allow users to upload or select sample documents that guide PDF split boundary
detection. The system computes perceptual hashes (phash) for fast similarity
comparison, falls back to LLM vision comparison for borderline cases, and augments
the existing SplitDetector results with sample-based boundaries.

---

## User Workflow

1. User navigates to Split tab
2. Uploads a bulk PDF (or selects existing one from job list)
3. **Provides sample document(s)** — either:
   - **Upload** up to 5 sample PDFs/images (drag & drop, file picker)
   - **Select from bulk** — opens page selector modal, user selects a page range,
     clicks "Add as Sample"
4. Adjusts similarity threshold slider (default 0.7)
5. Clicks "Detect Splits with Samples"
6. System renders all pages, computes hashes, runs comparison, runs SplitDetector,
   merges boundaries
7. Review modal shows results with sample match columns + full page viewer

---

## New Module: `app/sample_matcher.py`

### Classes

| Class | Responsibility |
|-------|---------------|
| `SampleManager` | Store, render, retrieve sample documents (up to 5 per job) |
| `SimilarityEngine` | Compute phash, sliding-window comparison, map distance → confidence |
| `LLMFallbackComparer` | Send borderline page pairs to Ollama vision model for verification |

### Sample Storage

```
data/samples/{job_id}/
  sample_0/
    meta.json          # {name, page_count, source: "upload"|"bulk"}
    page_0.png
    page_1.png
  sample_1/
    ...
  hashes_cache.json    # {sample_id: {page_n: "hexhash"}}
```

- Persisted in Docker volume (`./data/samples/`) — survives container restarts
- Up to 5 samples per job
- Rendered at `split_dpi` (150 dpi default)
- Cleaned when samples are removed or job is deleted

### Algorithm: `SimilarityEngine`

```
1. Render sample pages and bulk pages at split_dpi
2. Compute phash (64-bit perceptual hash) for every page via imagehash
3. For each sample (N pages):
   a. Slide N-page window across bulk pages
   b. Compute average hamming distance across window pages vs sample pages
   c. Map distance to confidence: confidence = max(0, 1 - distance / 30)
   d. Record match if confidence >= threshold
4. For borderline matches (confidence 0.5-0.8) → LLM vision fallback
5. Derive boundaries from match transitions:
   - Page i matches sample A, page i-1 does not → boundary at i-1
   - Page i matches sample A, page i-1 matches sample B → boundary at i-1
6. Union with SplitDetector boundaries, deduplicate
7. Return merged boundaries with match_source per boundary
```

### Confidence Mapping (Hamming Distance → 0-1)

| Hamming Distance | Confidence | Interpretation |
|-----------------|------------|----------------|
| 0 | 1.00 | Identical |
| 1–5 | 0.83–0.97 | Very similar |
| 6–10 | 0.67–0.80 | Similar (possible same doc type) |
| 11–15 | 0.50–0.63 | Borderline (LLM fallback triggers) |
| 16–20 | 0.33–0.47 | Weak (below threshold) |
| >20 | <0.33 | Different |

### LLMFallbackComparer

```
Prompt: "Are the following two pages from the same document type?
Return JSON: {"same": true/false, "confidence": 0.0-1.0}"

Images: Two page PNGs sent as base64 to Ollama vision model
Model: Uses OCR_MODEL (glm-ocr) by default
```

---

## API Endpoints

### New

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/jobs/{jid}/split-samples/upload` | Upload sample files (multipart, up to 5) |
| `POST` | `/api/jobs/{jid}/split-samples/from-bulk` | Select page range from bulk `{name, start_page, end_page}` |
| `GET` | `/api/jobs/{jid}/split-samples` | List all samples for this job |
| `DELETE` | `/api/jobs/{jid}/split-samples/{sid}` | Remove one sample |
| `DELETE` | `/api/jobs/{jid}/split-samples` | Clear all samples |
| `GET` | `/api/jobs/{jid}/split-bulk-thumbnails?page=N` | Bulk page thumbnail for selection modal |

### Modified

| Method | Path | Change |
|--------|------|--------|
| `POST` | `/api/jobs/{jid}/split-detect` | If samples exist, run sample comparison alongside normal detection; return merged results with `match_source` |
| `GET` | `/api/jobs/{jid}/split-preview` | Add `match_source`, `matched_sample` per child |
| `POST` | `/api/jobs/{jid}/split-confirm` | Set child status to `AWAITING_APPROVAL` instead of enqueuing |
| `GET` | `/api/jobs/{jid}/split-review` | New return type with `page_urls[]`, `cached` flag, full review data |
| `GET` | `/api/jobs/{jid}/split-page?page=N` | Serve cached page PNG (new endpoint) |
| `DELETE` | `/api/jobs/{jid}/split-cache` | Clear render cache (new endpoint) |

### `/split-detect` Extended Response

```json
{
  "boundaries": [3, 7],
  "confidences": [0.92, 0.88],
  "blank_pages": [5],
  "engine": "hybrid",
  "sample_matches": [
    {"boundary": 3, "matched_sample": 0, "confidence": 0.92, "source": "phash"},
    {"boundary": 7, "matched_sample": 1, "confidence": 0.75, "source": "llm"}
  ],
  "total_pages": 12
}
```

### `/split-preview` Extended Response

```json
{
  "boundaries": [3, 7],
  "confidences": [0.92, 0.88],
  "children": [
    {
      "index": 1,
      "start_page": 1,
      "end_page": 3,
      "page_count": 3,
      "confidence": 0.92,
      "matched_sample": 0,
      "sample_confidence": 0.92,
      "match_source": "phash"
    },
    {
      "index": 2,
      "start_page": 4,
      "end_page": 7,
      "page_count": 4,
      "confidence": 0.88,
      "matched_sample": 1,
      "sample_confidence": 0.75,
      "match_source": "llm"
    },
    {
      "index": 3,
      "start_page": 8,
      "end_page": 12,
      "page_count": 5,
      "confidence": null,
      "matched_sample": null,
      "sample_confidence": null,
      "match_source": null
    }
  ],
  "total_pages": 12
}
```

---

## Config Additions

Add to `Settings` in `app/config.py`:

```python
split_sample_threshold: float = Field(default=0.7, ge=0.0, le=1.0,
    description="Phash similarity threshold for sample matching")
split_sample_llm_fallback: bool = Field(default=True,
    description="Use LLM vision for borderline phash matches")
split_sample_max_count: int = Field(default=5, ge=1, le=10,
    description="Maximum number of samples per split job")
```

UI Settings tab exposes: `split_sample_threshold`, `split_sample_llm_fallback` (not `max_count` — hard limit).

---

## Review Modal (Frontend)

### Sample Thumbnail Section (above page strip)

```
┌────────────────────────────────────────────────────────────┐
│  Samples:  [Sample 1]  [Sample 2]  [+ Add Sample]         │
│            3 pages       1 page     [Upload] [From Bulk]   │
│  Threshold: [========|==========] 0.7                      │
│  [Detect Splits with Samples]                              │
└────────────────────────────────────────────────────────────┘
```

### "Select from Bulk" Modal

```
┌────────────────────────────────────────────────────────────┐
│  Select Pages from Bulk PDF — document.pdf    [× Close]    │
├────────────────────────────────────────────────────────────┤
│  [1] [2] [3] [4] [5] [6] [7] [8] [9] ...                 │
│  ┌───┐ ┌───┐ ┌───┐                                       │
│  │Sel│ │Sel│ │Sel│  ← Selected pages highlighted          │
│  └───┘ └───┘ └───┘                                       │
│                                                             │
│  Pages 2–4 selected (3 pages)                              │
│  Sample name: [Invoice 1        ]                          │
│                                                             │
│  [Cancel]                            [Add as Sample]       │
└────────────────────────────────────────────────────────────┘
```

- Grid of all bulk page thumbnails (rendered at thumbnail size)
- Click to start selection, click again to end → range highlighted with border
- Shows "Pages X–Y selected (N pages)"
- Name input auto-fills from page range: "Sample 1", "Sample 2", etc.
- Paging controls for large PDFs (e.g., show 20 thumbnails at a time)

### Review Modal Additions

- **Sample match column** — shows which sample each child matched + confidence
- **Sample icon/color** — visual indicator: green checkmark (phash match), blue star (LLM verified)
- **Live threshold slider** — adjusting it re-computes boundaries from cached hashes (no re-render)
  - Shows low-confidence boundaries disappearing/appearing as threshold changes
  - "Apply" button to commit threshold change and refresh boundaries

---

## Dependencies

Add to `requirements.txt`:
```
imagehash>=4.3.1
```

`imagehash` depends on `Pillow` (already in requirements) and `numpy` (already pulled by other deps).

---

## Cache Structure (combined)

```
/tmp/docuforge/renders/{job_id}/
  pages/
    page_0.png       ← Rendered at split_dpi for page viewer
    page_1.png
    ...
  thumbnails/
    child_1.png      ← First page of each child doc
    child_2.png

data/samples/{job_id}/
  sample_0/
    meta.json
    page_0.png
  hashes_cache.json   ← Precomputed phash values for all pages
```

---

## Pipeline Changes

### Remove from `process_file()` (`app/processor.py`)

- Lines 401–481: Entire Step 1.5 split detection block
- Auto-split branch (lines 437–462)
- `AWAITING_SPLIT_APPROVAL` status: no longer set by pipeline

### What stays

- `JobData.split_phase` / `split_progress_pct` — still set by `/split-detect`
- `JobData.job_type` ("split_parent" / "split_child") — grouped by UI
- `JobData.split_boundaries`, `split_confidences`, `blank_pages_removed`
- `AWAITING_SPLIT_APPROVAL` enum value — used by batch-approve check

### `/split-confirm` Behavior Change

**Before:**
```python
child_job = job_manager.create_job(child_path.name)
job_queue.enqueue(child_job.id, child_path.resolve())  # auto-processed
```

**After:**
```python
child_job = job_manager.create_job(child_path.name)
child_job.status = JobStatus.AWAITING_APPROVAL  # pause for review
child_job.job_type = "split_child"
child_job.parent_job_id = job_id
# NOT enqueued — user must approve via batch-approve UI
```

Children are visible in Jobs list under parent. User approves via batch-approve,
which enqueues them into the processing pipeline.

---

## Files Changed

| File | Changes |
|------|---------|
| `app/sample_matcher.py` | **Create** — SampleManager, SimilarityEngine, LLMFallbackComparer |
| `app/main.py` | New sample endpoints. `/split-detect`, `/split-preview`, `/split-confirm` modified. `/split-review`, `/split-page`, `/split-cache` added. |
| `app/processor.py` | Remove Step 1.5 block (lines 401–481). `AWAITING_APPROVAL` used by split children. |
| `app/config.py` | Add `split_sample_threshold`, `split_sample_llm_fallback`, `split_sample_max_count` |
| `app/templates/index.html` | Sample UI section, select-from-bulk modal, review modal with page strip/viewer/handles, sample match column, threshold slider |
| `app/static/style.css` | Modal overlay, sample card, page selection grid, handle styles, match indicator styles |
| `requirements.txt` | Add `imagehash>=4.3.1` |

---

## Edge Cases

- No samples provided → detect runs normally via SplitDetector only (no sample comparison)
- Samples uploaded but bulk not selected → detect button disabled
- Sample is same as part of bulk → phash detects near-identical, LLM fallback confirms
- LLM vision model unavailable → phash-only mode, borderline matches logged as "unverified"
- Container restart → render cache wiped (`/tmp`), sample data survives (`data/samples/`)
- User changes threshold after detection → re-compute boundaries from cached hashes (no re-render)
- Max 5 samples enforced — attempt to add 6th shows error toast
- Removing a sample mid-review → boundaries recomputed without that sample's matches
