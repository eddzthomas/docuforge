# GLM-OCR Integration Plan for DocuForge

## Why GLM-OCR Failed Via Ollama

The Ollama deployment (`glm-ocr:latest`, 1.1B params) is a **raw vision model** — it lacks the layout detection stage. GLM-OCR requires a two-stage pipeline:

```
Page Image → PP-DocLayout-V3 → Cropped Text Regions → GLM-OCR Model → Recognized Text
              (layout detection)                         (region OCR)
```

Without layout detection, the raw model sees an entire page and returns garbage.  
The Ollama deployment guide itself says it's for **"Testing/Personal Use" only**.

---

## Deployment Options (Ranked by Quality)

| # | Option | GPU Needed | OCR Quality | Setup Complexity | Best For |
|---|--------|------------|-------------|-------------------|----------|
| 1 | **vLLM + GLM-OCR SDK** (self-hosted) | Yes (10GB+ VRAM) | Maximum (94.62 OmniDocBench) | Medium | Production, full control |
| 2 | **SGLang + GLM-OCR SDK** (self-hosted) | Yes (10GB+ VRAM) | Maximum | Medium | Production, alternative to vLLM |
| 3 | **SDK Server + Client** | GPU on server only | Maximum | Medium | Split GPU/CPU deployment |
| 4 | **Zhipu MaaS Cloud API** | None (cloud) | Maximum | Low | No GPU, pay-per-use |
| 5 | **Ollama (current)** | Optional (CPU/GPU) | Very poor | Already done | Not recommended |

---

## Recommended Path: vLLM + SDK Server

### Architecture

```
┌────────────────────────────┐       HTTP        ┌───────────────────────────────┐
│  DocuForge Container       │ ────────────────→ │  vLLM Container (GPU)         │
│                            │  OCR API calls    │                               │
│  app/ocr.py                │                   │  Model: zai-org/GLM-OCR (BF16)│
│  → glmocr SDK client mode  │                   │  Port: 8080                   │
│  → or REST HTTP calls      │                   │  ~8-10 GB VRAM                │
└────────────────────────────┘                   └───────────────────────────────┘
                                                             │
                                                   ┌─────────▼──────────┐
                                                   │  GLM-OCR SDK       │
                                                   │  (layout detection │
                                                   │   + parallel OCR)  │
                                                   └────────────────────┘
```

### How It Works

1. **vLLM** serves the full `zai-org/GLM-OCR` model (BF16, ~0.9B params) with MTP speculative decoding
2. **DocuForge** sends each page image to vLLM's OpenAI-compatible `/v1/chat/completions` endpoint (or the GLM-OCR native API)
3. **The SDK** (or our code) handles:
   - Layout detection via PP-DocLayout-V3 (can run on CPU)
   - Cropping text regions from the page
   - Sending each region to vLLM for OCR
   - Aggregating results with bounding boxes
4. **DocuForge** receives structured text + bbox data and layers it onto the PDF

### GPU Requirements

| Component | VRAM | Notes |
|-----------|------|-------|
| GLM-OCR model (BF16) | ~2 GB | 0.9B params × 2 bytes |
| vLLM overhead (KV cache) | ~2-4 GB | Depends on context length |
| Layout detection (PP-DocLayoutV3) | 0 GB | Can run on CPU |
| **Total minimum** | **~6-8 GB** | Consumer GPU (RTX 3060/4060) |

vLLM supports `--gpu-memory-utilization 0.85` to fit in tighter VRAM budgets.

---

## Implementation Plan

### Phase 1: Set Up vLLM (Separate Docker Container)

Create a new service in `docker-compose.yml` for vLLM:

```yaml
services:
  vllm-glm-ocr:
    image: vllm/vllm-openai:v0.19.0-ubuntu2404
    container_name: docuforge-vllm
    ports:
      - "8081:8080"
    command: >
      --model zai-org/GLM-OCR
      --port 8080
      --served-model-name glm-ocr
      --speculative-config '{"method": "mtp", "num_speculative_tokens": 3}'
      --gpu-memory-utilization 0.85
      --max-model-len 8192
    volumes:
      - vllm-cache:/root/.cache/huggingface
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks:
      - docuforge-net

volumes:
  vllm-cache:
```

**Note:** This service is separate from the existing `ollama` service. Users choose one:
- GPU machine → vLLM + GLM-OCR (high quality)
- CPU machine → Tesseract (basic)
- Both can run simultaneously on the same GPU machine

### Phase 2: Add GLM-OCR vLLM Backend to `app/ocr.py`

Add a new OCR backend that calls the vLLM endpoint:

```python
# New config option
OCR_ENGINE = "glm-ocr-vllm"  # alternatives: tesseract, glm-ocr-ollama

VLLM_HOST = "http://vllm-glm-ocr:8080"  # Docker service name
```

**API call:** Use the OpenAI-compatible chat completions API:

```python
POST http://vllm-glm-ocr:8080/v1/chat/completions
{
    "model": "glm-ocr",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract all text from this document with bounding boxes..."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            ]
        }
    ]
}
```

**Response parsing:** The vLLM model returns text with bounding box metadata. Parse into our standard `{full_text, words: [{text, bbox}]}` format.

**Alternative approach:** Use the GLM-OCR SDK directly in DocuForge's code:

```python
# In app/ocr.py
from glmocr import GlmOcr

async def _ocr_page_glm_vllm(image_path, settings):
    with GlmOcr(
        mode="selfhosted",
        api_host=settings.vllm_host,
        api_port=settings.vllm_port,
    ) as parser:
        result = parser.parse(str(image_path))
        return _convert_sdk_result_to_standard(result)
```

This approach uses the SDK's two-stage pipeline (layout detection + OCR) automatically. However, the SDK requires:
- `pip install "glmocr[selfhosted]"` — adds layout detection dependencies (PaddleOCR/PP-DocLayoutV3)
- CPU resources for layout detection
- Python 3.12+ (we're on 3.11 — need to upgrade the Docker base image)

### Phase 3: Docker Integration Decision

**Option A — SDK integrated into DocuForge container:**
- Pros: Single container handles everything
- Cons: Need Python 3.12, heavy dependencies (PaddlePaddle for layout detection)
- Image size: +2-3 GB for layout detection dependencies

**Option B — Separate vLLM + Layout Detection server:**
- Pros: DocuForge stays light, GPU scheduling is separate
- Cons: Two containers, HTTP overhead
- Layout detection could run on CPU in a third container or on the same GPU machine

**Option C — Use vLLM REST API directly (simplest):**
- Send page images directly to vLLM's `/v1/chat/completions`
- Prompt the model to do layout analysis + OCR in one pass
- No SDK dependency, no layout detection container
- May be slightly less accurate than the two-stage pipeline

**Recommended for initial implementation: Option C**
- Simple REST API call (like current Ollama integration)
- No new Python dependencies
- Can upgrade to Option A/B later when GPU is available

### Phase 4: Code Changes Required

| File | Change | Lines |
|------|--------|-------|
| `app/ocr.py` | Add `_ocr_page_glm_vllm()` backend | ~40 |
| `app/config.py` | Add `VLLM_HOST`, `VLLM_MODEL` settings | ~5 |
| `docker-compose.yml` | Add `vllm-glm-ocr` service (conditional) | ~25 |
| `docker-compose.gpu.yml` | Already exists, reference vLLM there | ~10 |
| `.env.example` | Document new VLLM settings | ~5 |

### Phase 5: Config Options

```env
# OCR Engine: tesseract | glm-ocr-ollama | glm-ocr-vllm
OCR_ENGINE=glm-ocr-vllm

# vLLM server (only used when OCR_ENGINE=glm-ocr-vllm)
VLLM_HOST=http://vllm-glm-ocr:8080
VLLM_MODEL=glm-ocr

# Layout detection (future SDK integration)
GLMOCR_LAYOUT_DETECTION=true
```

---

## Implementation Order

### Iteration 1: vLLM REST API Integration (This Sprint)
- Add vLLM service to docker-compose (GPU machines only)
- Add `glm-ocr-vllm` OCR backend using OpenAI-compatible API
- Test with sample documents
- Works on: GPU machines with 8GB+ VRAM
- DocuForge container stays lightweight

### Iteration 2: SDK Integration (Future)
- Upgrade Docker base to Python 3.12
- Install `glmocr[selfhosted]` for layout detection pipeline
- Use SDK's two-stage pipeline for maximum accuracy
- Works on: GPU machines with 10GB+ VRAM

### Iteration 3: Split Deployment (Future)
- Run layout detection in a separate CPU container
- Run vLLM in GPU container
- DocuForge orchestrates both
- Benefits: Layout detection doesn't compete with OCR for GPU memory

---

## Fallback Strategy

If vLLM is unavailable (no GPU, not running):
1. Try GLM-OCR via Ollama (current, poor quality)
2. Fall back to Tesseract (always works)
3. Log warnings and continue

The `ocr_page()` dispatcher already supports this pattern.

---

## Verification Criteria

| Criterion | Current (Tesseract) | Target (GLM-OCR vLLM) |
|-----------|---------------------|------------------------|
| Text accuracy on English docs | ~95% | ~98%+ |
| Text accuracy on complex layouts | ~80% | ~94%+ |
| Table recognition | No | Yes (structured output) |
| Formula recognition | No | Yes |
| Handwriting | Minimal | Good |
| Multi-language | English only | Multi-language (model supports) |
| Speed per page | ~1s (CPU) | ~3-5s (GPU) |
| Bounding boxes | Word-level | Region + word-level |
| GPU required | No | Yes |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| vLLM model download (2GB+) | HuggingFace cache volume persists |
| vLLM container fails to start | Fall back to Tesseract automatically |
| SDK dependencies break | Use REST API approach (no SDK dependency) |
| GPU OOM (out of memory) | `--gpu-memory-utilization 0.75` conservative |
| Python 3.12 requirement (SDK) | Use REST API approach until Docker base upgrade |
| Layout detection not available | Prompt model to OCR full page (less accurate but works) |
