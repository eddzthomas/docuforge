# Split Review & Manual Edit — Implementation Plan

> **Status:** Partially implemented. The `openSplitPreview` modal (table-based child review) is
> complete. Remaining work: remove Step 1.5 from pipeline, add page viewer/review endpoints,
> change `/split-confirm` to `AWAITING_APPROVAL`, and add the full review modal with page strip
> and handles.
>
> **Extended by:** [SAMPLE_MATCHING_PLAN.md](SAMPLE_MATCHING_PLAN.md) — adds sample-guided
> detection (perceptual hash + LLM vision fallback) with select-from-bulk and threshold slider.

## Goal
Decouple split detection from the upload/pipeline. The Split tab becomes the sole path
for split detection + manual review + confirmation. Child jobs pause at AWAITING_APPROVAL
after confirmation.

---

## Pipeline Changes

### Remove from `process_file()` (Step 1.5 block)

- Lines 401–481: Entire split detection block removed
- Auto-split logic (lines 437–462): Removed
- `AWAITING_SPLIT_APPROVAL` status: Still defined (backward compat) but no longer set by pipeline
- `job_type="split_parent"` / `split_child"`: Kept (used by UI to group children under parent)

### What stays on JobData
`split_phase` / `split_progress_pct` remain on JobData — they are still set by the
Split tab's `/split-detect` endpoint for real-time progress display. Only `process_file()`
no longer sets them.

### Steps (unchanged)
Steps 1 (normalize), 2 (render), 3 (OCR), 3.5 (classify), 3.6 (verify), 4 (PDF/A),
5 (layer text), 5.5 (extract fields), 6 (LLM rename/tag), 7 (finalize) — no renumbering.
The split gate is simply gone.

---

## New API Endpoints

### `GET /api/jobs/{job_id}/split-review`
Returns the data needed to render the review modal.

**Response:**
```json
{
  "job_id": "...",
  "page_count": 12,
  "boundaries": [3, 7],
  "confidences": [0.92, 0.88],
  "children": [
    {
      "index": 1,
      "start_page": 1,
      "end_page": 3,
      "page_count": 3,
      "confidence": 0.92
    },
    {
      "index": 2,
      "start_page": 4,
      "end_page": 7,
      "page_count": 4,
      "confidence": 0.88
    },
    {
      "index": 3,
      "start_page": 8,
      "end_page": 12,
      "page_count": 5,
      "confidence": null
    }
  ],
  "page_urls": [
    "/api/jobs/{id}/split-page?page=0",
    "/api/jobs/{id}/split-page?page=1",
    ...
  ],
  "blank_pages": [5],
  "cached": true
}
```

- `page_urls[i]` is always present (0-indexed)
- `children` uses 1-indexed page numbers for display
- Last child segment has no boundary — its `confidence` is null

**Errors:**
- 404 if job not found
- 400 if job has no file_path
- 400 if not a PDF

### `GET /api/jobs/{job_id}/split-page?page=N`
Serves a cached rendered page PNG. N is 0-indexed.

**Response:** PNG image (Content-Type: image/png)

**Errors:**
- 400 if page out of range
- 404 if cache missing (must call /split-detect first)

### `DELETE /api/jobs/{job_id}/split-cache`
Cleans up cached render data after review is done or cancelled.

**Response:** `{ "cleaned": true }`

---

## Cache Structure

```
/tmp/docuforge/renders/{job_id}/
  pages/
    page_0.png
    page_1.png
    ...
  thumbnails/
    child_1.png
    child_2.png
    ...
```

- Renders at `split_dpi` (150 dpi by default)
- Survives across API calls within a container session
- Not a Docker volume — cleaned on container restart
- Cleaned on `/split-confirm` or `/split-cache` DELETE

---

## Split Detection (modified)

`POST /api/jobs/{job_id}/split-detect` is updated to save rendered pages to the
job cache directory so `/split-page` can serve them:

- Renders pages at `split_dpi` via `pdf_to_images()`
- Creates `/tmp/docuforge/renders/{job_id}/pages/page_0.png ... page_N.png`
- Runs SplitDetector
- Saves thumbnails to `/tmp/docuforge/renders/{job_id}/thumbnails/`
- Updates job with `split_boundaries`, `split_confidences`, `blank_pages_removed`
- Returns `{boundaries, confidences, blank_pages, engine}`

The cache directory is created fresh per detection run.

---

## Confirmation Flow (modified at `/split-confirm`)

**Request body extended:**
```json
{
  "boundaries": [3, 7]   // optional — if provided, overrides job.split_boundaries
}
```

**Before:**
```python
job_queue.enqueue(child_job.id, child_path.resolve())  # auto-proceed
```

**After:**
```python
# Optional: accept boundaries from the review modal (client-edited)
if request_body.get("boundaries") is not None:
    job_manager.update_job(job_id, split_boundaries=request_body["boundaries"], split_confidences=[])

# Child jobs created at AWAITING_APPROVAL — not enqueued
child_job.status = JobStatus.AWAITING_APPROVAL

# Cache auto-cleaned on success (same logic as /split-cache DELETE)
```

Children are visible in the Jobs list under the parent. User approves via the Approve
tab (or batch-approve). When approved, batch-approve enqueues them.

---

## Frontend: Review Modal

### Trigger
After `/split-detect` returns successfully, the Split tab shows a "Review Splits" button.
Clicking it opens the review modal overlay.

### Layout
```
┌─────────────────────────────────────────────────────┐
│  Split Review — document.pdf          [× Close]    │
├─────────────────────────────────────────────────────┤
│  ● ○ ○ ● ○ ○ ○ ○ ○ ○ ○ ○    ← page strip          │
│  [1]  [2]  [3]  [4] ...       ← page numbers      │
│  ┃    ┃         ┃            ← split handles     │
│  ┃    ┃         ┃  ← draggable; colored by conf  │
│                                                     │
│  [Prev]  page 3 of 12  [Next]   ← navigation      │
│                                                     │
│  Confidence: 0.92 (high) at page 3                 │
│  [Remove Split]  [Add Split After Page 3]         │
├─────────────────────────────────────────────────────┤
│  Splits: 2 detected  │  Pages: 12  │  [Cancel]    │
│                                        [Confirm]    │
└─────────────────────────────────────────────────────┘
```

### Interactions
- **Click a handle** → Remove split (toggles off)
- **Click between pages** → Add split at that boundary
- **Page strip** → Horizontal scroll through all pages at thumbnail size
- **Full page view** → Larger view of single page (navigated with Prev/Next)
- **Confidence coloring** → Green handle ≥ split_confidence; Yellow < split_confidence
- **Confirm** → POST `/split-confirm` with final boundaries in body, auto-cleans cache, closes modal
- **Close/Cancel** → DELETE `/split-cache`, close modal (boundaries remain on job from last PUT or auto-detect)

### State
- Client keeps `boundaries[]` array in memory during review
- PUT `/split-points` called only if user explicitly saves adjustments (debounced, not on every click)
- On modal open: GET `/split-review` fetches current boundaries from server
- Modal send-final boundaries in `/split-confirm` body on confirm (no intermediate sync needed)

---

## Files Changed

| File | Changes |
|------|---------|
| `app/main.py` | New `/split-review`, `/split-page`, `/split-cache` routes. `/split-detect` updated to copy rendered pages to job cache dir. `/split-confirm` updated to NOT enqueue children and accept optional `boundaries` body param. |
| `app/processor.py` | Remove Step 1.5 block. Remove auto-split branch. `split_phase`/`split_progress_pct` stay on JobData (used by `/split-detect`). |
| `app/templates/index.html` | Review modal overlay in Split tab. Page strip, full page view, handle interaction. |
| `app/static/style.css` | Modal overlay styles. Split handle styles. Confidence color classes. |
| `app/config.py` | No changes |
| `app/splitter.py` | No changes |

---

## Backward Compatibility
- `AWAITING_SPLIT_APPROVAL` status kept in JobStatus enum (still used by batch-approve check at line 966 of main.py)
- Existing jobs that are already at `AWAITING_SPLIT_APPROVAL` can still be confirmed via `/split-confirm`
- `job_type="split_parent"` / `split_child"` still set and can be used by UI grouping

---

## Edge Cases
- If `/split-review` called before `/split-detect` → 400 with message "No split cache found — run split-detect first"
- If cache directory is missing when `/split-page` called → 404
- If page param out of range → 400
- If `/split-confirm` called with no boundaries → splits whole PDF into 1 child (no-op for single-page PDFs)
- Container restart wipes cache — user must re-run detect if they had pending review