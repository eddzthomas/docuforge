# DocuForge — Model Compatibility Matrix

Which Ollama models work for which DocuForge features, and what VRAM they need.

---

## Feature-to-Model Mapping

| Feature | Requires Vision? | Model Used | Config Key |
|---------|:---:|--------|-------------|
| **OCR** (GLM-OCR engine) | Yes | `glm-ocr` | `OCR_MODEL` |
| **OCR** (Tesseract) | No | — (CPU) | `OCR_ENGINE=tesseract` |
| **Rename & Tag** | No | `TAGGING_MODEL` | `TAGGING_MODEL` |
| **Document Classification** | No | `TAGGING_MODEL` (shared) | `CLASSIFY_MODEL` |
| **Field Extraction** (invoices) | No | `TAGGING_MODEL` (shared) | `EXTRACT_MODEL` |
| **Split Detection** (vision pass) | Yes | `OCR_MODEL` (reused) | `SPLIT_MODEL` |
| **Sample LLM Fallback** | Yes | `TAGGING_MODEL` or `OCR_MODEL` | (fallback logic) |

> **Note:** `TAGGING_MODEL` powers 4 features — rename, tag, classify, and field extraction.  
> Setting it to a vision-capable model like `gemma4:e2b` also enables LLM fallback for sample matching  
> without needing a separate model.

---

## Recommended Models

### `gemma4:e2b` — All-Purpose Workhorse

| Property | Value |
|----------|-------|
| Size | 7.2 GB (Q4_K_M) |
| Context | 128K tokens |
| Modalities | Text, Image, Audio |
| Thinking mode | Yes (configurable) |
| VRAM needed | 6–8 GB |

**Serves:** Rename, tag, classify, extract, sample LLM fallback.  
**Best for:** Users with 6GB+ VRAM who want a single model for everything.  
**Not suitable for:** OCR (use glm-ocr for that).

```
ollama pull gemma4:e2b
```

Env: `TAGGING_MODEL=gemma4:e2b`

### `glm-ocr` — OCR Specialist

| Property | Value |
|----------|-------|
| Size | ~4 GB |
| Context | 4K (vision only) |
| Modalities | Image |
| VRAM needed | 4–6 GB |

**Serves:** OCR (all engines), split detection vision pass.  
**Best for:** OCR — still the best single-purpose OCR model.

```
ollama pull glm-ocr
```

Env: `OCR_MODEL=glm-ocr`

### `gemma3:12b` — Alternative Workhorse

| Property | Value |
|----------|-------|
| Size | 8.1 GB (Q4_K_M) |
| Context | 128K tokens |
| Modalities | Text, Image |
| VRAM needed | 8–10 GB |

**Serves:** Same as gemma4:e2b (all text + vision tasks).  
**Best for:** Users who need maximum parameter count on 8GB+ VRAM.

```
ollama pull gemma3:12b
```

### `llama3.2` — Lightweight Fallback

| Property | Value |
|----------|-------|
| Size | ~2 GB |
| Context | 128K tokens |
| Modalities | Text only |
| VRAM needed | 2–4 GB |

**Serves:** Rename, tag, classify, extract (text only).  
**Best for:** Low-VRAM setups. Does NOT support vision (no sample LLM fallback, no OCR).

```
ollama pull llama3.2
```

---

## VRAM Budget Calculator

| Setup | OCR | Tag/Classify/Extract | Total VRAM |
|-------|-----|---------------------|------------|
| **Minimal** | tesseract (0) | llama3.2 (2GB) | **2 GB** |
| **Standard** | glm-ocr (4GB) | llama3.2 (2GB) | **6 GB** |
| **Recommended** | glm-ocr (4GB) | gemma4:e2b (7.2GB) | **11.2 GB** ✝ |
| **Max Power** | glm-ocr (4GB) | gemma3:12b (8.1GB) | **12.1 GB** ✝ |

> ✝ Only one model is active at a time in DocuForge's single-concurrency queue.  
> Real peak VRAM ≈ max(model_A, model_B) + overhead, not sum.

---

## Model Selection Guide

### "I have 4GB VRAM"

- OCR: `tesseract` (CPU) — no VRAM cost
- Tag/classify: `llama3.2` (2GB)
- Split vision: disabled (`split_engine=heuristic`)
- Sample matching: phash only (no LLM fallback)

### "I have 6GB VRAM"

- OCR: `glm-ocr` (4GB)
- Tag/classify: `llama3.2` (2GB)
- Split vision: uses glm-ocr (already loaded)
- Sample LLM fallback: uses glm-ocr (already loaded)

### "I have 8GB+ VRAM" (Recommended)

- OCR: `glm-ocr` (4GB)
- Tag/classify/extract: `gemma4:e2b` (7.2GB) — one model for everything
- Split vision: uses glm-ocr
- Sample LLM fallback: uses gemma4:e2b
- Set `TAGGING_MODEL=gemma4:e2b`

### "I have 12GB+ VRAM"

- OCR: `glm-ocr` (4GB)
- Tag/classify/extract: `gemma3:12b` (8.1GB) or `gemma4:e4b` (9.6GB)
- Everything else as above

---

## Vision Model Benchmarks (Relevance to DocuForge)

| Model | DocVQA | OmniDocBench 1.5 | Notes |
|-------|--------|-----------------|-------|
| `gemma4:e2b` | — | 0.290 | Excellent document parsing. Audio support. |
| `gemma3:12b` | 82.3 | — | Strong general-purpose vision. |
| `gemma3:4b` | 72.8 | — | Good enough for simple docs. |
| `glm-ocr` | — | — | Purpose-built for OCR. No public benchmark. |

> OmniDocBench 1.5 = average edit distance (lower is better). Tests document understanding.  
> DocVQA = visual question answering on documents (higher is better).

---

## Docker Auto-Pull

```bash
# Pull models into the running Ollama container
docker exec -it docuforge-ollama ollama pull glm-ocr
docker exec -it docuforge-ollama ollama pull gemma4:e2b
docker exec -it docuforge-ollama ollama pull llama3.2
```

Or set in `.env`:
```
OLLAMA_AUTO_PULL=true
OLLAMA_AUTO_PULL_MODELS=glm-ocr,gemma4:e2b,llama3.2
```
