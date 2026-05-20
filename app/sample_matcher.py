"""
DocuForge — Sample Matcher
===========================
Sample-guided split detection using perceptual hashing (phash) with
LLM vision fallback for borderline matches.

Classes:
    SampleManager — Store, render, retrieve sample documents (up to 5 per job).
    SimilarityEngine — Compute phash, sliding-window comparison, map distance → confidence.
    LLMFallbackComparer — Send borderline page pairs to Ollama vision model.
"""

import json
import logging
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

SAMPLES_BASE = Path("/data/samples")


# =============================================================================
# SampleManager
# =============================================================================


class SampleManager:
    """
    Manages sample document storage for a given split job.

    Storage layout:
        data/samples/{job_id}/
            sample_0/
                meta.json          # {name, page_count, source: "upload"|"bulk"}
                page_0.png
                page_1.png
            sample_1/
                ...
            hashes_cache.json

    Args:
        job_id: The split parent job ID this sample set belongs to.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.root = SAMPLES_BASE / job_id
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- Sample CRUD ----

    def add_sample_from_upload(self, page_paths: list[Path], name: str = "") -> dict:
        """
        Register a sample from uploaded/referenced page images.

        Args:
            page_paths: Ordered list of Paths to rendered sample pages (PNG).
            name: Human-readable label for the sample.

        Returns:
            dict with keys: id, name, page_count, source.
        """
        sample_id = self._next_sample_id()
        sample_dir = self.root / f"sample_{sample_id}"
        sample_dir.mkdir(parents=True, exist_ok=True)

        for i, src in enumerate(page_paths):
            dst = sample_dir / f"page_{i}.png"
            dst.write_bytes(src.read_bytes())

        meta = {
            "name": name or f"Sample {sample_id + 1}",
            "page_count": len(page_paths),
            "source": "upload",
        }
        (sample_dir / "meta.json").write_text(json.dumps(meta))

        return {"id": f"sample_{sample_id}", "name": meta["name"],
                "page_count": meta["page_count"], "source": meta["source"]}

    def add_sample_from_bulk(self, images: list[Image.Image], name: str,
                             start_page: int, end_page: int) -> dict:
        """
        Register a sample selected from the bulk PDF.

        Args:
            images: List of PIL Images for the selected page range.
            name: Human-readable label.
            start_page: 0-indexed start page in the bulk.
            end_page: 0-indexed end page (inclusive) in the bulk.

        Returns:
            dict with keys: id, name, page_count, source, start_page, end_page.
        """
        sample_id = self._next_sample_id()
        sample_dir = self.root / f"sample_{sample_id}"
        sample_dir.mkdir(parents=True, exist_ok=True)

        for i, img in enumerate(images):
            page_path = sample_dir / f"page_{i}.png"
            img.save(str(page_path), "PNG")

        meta = {
            "name": name or f"Sample {sample_id + 1}",
            "page_count": len(images),
            "source": "bulk",
            "start_page": start_page,
            "end_page": end_page,
        }
        (sample_dir / "meta.json").write_text(json.dumps(meta))

        return {"id": f"sample_{sample_id}", "name": meta["name"],
                "page_count": meta["page_count"], "source": meta["source"],
                "start_page": start_page, "end_page": end_page}

    def list_samples(self) -> list[dict]:
        """Return metadata for all registered samples."""
        samples = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or not child.name.startswith("sample_"):
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text())
            meta["id"] = child.name
            meta["page_paths"] = [
                str(p) for p in sorted(child.glob("page_*.png"))
            ]
            samples.append(meta)
        return samples

    def remove_sample(self, sample_id: str) -> bool:
        """Remove one sample and its directory. Returns True if removed."""
        import shutil
        sample_dir = self.root / sample_id
        if sample_dir.exists():
            shutil.rmtree(sample_dir)
            # Clear hash cache since it may reference this sample
            cache = self.root / "hashes_cache.json"
            if cache.exists():
                cache.unlink()
            return True
        return False

    def clear_all(self) -> None:
        """Remove all samples for this job."""
        import shutil
        if self.root.exists():
            shutil.rmtree(self.root)

    def get_page_paths(self, sample_id: str) -> list[Path]:
        """Return ordered list of page image Paths for a sample."""
        sample_dir = self.root / sample_id
        if not sample_dir.exists():
            return []
        return sorted(sample_dir.glob("page_*.png"))

    def get_page_count(self, sample_id: str) -> int:
        """Return number of pages in a sample."""
        meta_path = self.root / sample_id / "meta.json"
        if not meta_path.exists():
            return 0
        return json.loads(meta_path.read_text()).get("page_count", 0)

    # ---- Internal ----

    def _next_sample_id(self) -> int:
        existing = [d for d in self.root.iterdir() if d.is_dir() and d.name.startswith("sample_")]
        return len(existing)


# =============================================================================
# SimilarityEngine
# =============================================================================


def _phash_distance(h1: str, h2: str) -> int:
    """Compute hamming distance between two hex perceptual hashes."""
    if len(h1) != len(h2):
        return 64  # max distance if mismatch
    try:
        b1 = int(h1, 16)
        b2 = int(h2, 16)
        xor = b1 ^ b2
        return xor.bit_count()
    except ValueError:
        return 64


def _distance_to_confidence(distance: int) -> float:
    """Map hamming distance (0–64) to a 0–1 confidence score."""
    return max(0.0, 1.0 - distance / 30.0)


def _compute_hash(image: Image.Image) -> str:
    """Compute the perceptual hash of a PIL Image, returning a hex string."""
    import imagehash
    return str(imagehash.phash(image))


def compute_all_hashes(image_paths: list[Path]) -> list[str]:
    """Compute phash for every page image path. Returns list of hex strings."""
    hashes = []
    for p in image_paths:
        try:
            img = Image.open(str(p)).convert("L")
            hashes.append(_compute_hash(img))
        except Exception as exc:
            logger.warning(f"Failed to compute hash for {p}: {exc}")
            hashes.append("")
    return hashes


def compute_sample_hashes(sample_manager: SampleManager) -> dict:
    """
    Compute and cache phash values for all sample pages.
    Returns {sample_id: [hash_str, ...]}.
    """
    samples = sample_manager.list_samples()
    result = {}
    for s in samples:
        page_paths = [Path(p) for p in s.get("page_paths", [])]
        result[s["id"]] = compute_all_hashes(page_paths)

    cache_path = sample_manager.root / "hashes_cache.json"
    cache_data = json.dumps(result, default=str)
    cache_path.write_text(cache_data)
    return result


def load_cached_hashes(sample_manager: SampleManager) -> dict | None:
    """Load precomputed hashes from cache, or None if missing."""
    cache = sample_manager.root / "hashes_cache.json"
    if not cache.exists():
        return None
    try:
        return json.loads(cache.read_text())
    except (json.JSONDecodeError, OSError):
        return None


class SimilarityEngine:
    """
    Compare sample documents against bulk pages using perceptual hashing.

    For each sample with N pages, slides an N-page window across the bulk,
    computes average hamming distance, and maps to a 0–1 confidence score.

    Args:
        threshold: Minimum confidence (0–1) to consider a match.
    """

    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold

    def compare(self, sample_hashes: list[str],
                bulk_hashes: list[str]) -> list[dict]:
        """
        Compare one sample against all bulk pages using sliding window.

        Args:
            sample_hashes: List of hex phash strings for the sample's pages.
            bulk_hashes: List of hex phash strings for all bulk pages.

        Returns:
            List of match dicts: [{page_index, confidence, borderline}, ...].
        """
        n = len(sample_hashes)
        m = len(bulk_hashes)
        if n == 0 or m < n:
            return []
        if any(not h for h in sample_hashes):
            return []

        matches = []
        window_hashes = sample_hashes

        for i in range(m - n + 1):
            distances = []
            for j in range(n):
                bh = bulk_hashes[i + j]
                if not bh:
                    distances.append(64)
                    continue
                distances.append(_phash_distance(window_hashes[j], bh))
            avg_dist = sum(distances) / len(distances)
            confidence = _distance_to_confidence(int(avg_dist))

            if confidence >= self.threshold:
                borderline = 0.5 <= confidence < 0.8
                matches.append({
                    "page_index": i,
                    "confidence": round(confidence, 4),
                    "borderline": borderline,
                    "avg_hamming": round(avg_dist, 2),
                })

        # Deduplicate overlapping matches (keep highest confidence)
        return self._deduplicate(matches, n)

    def compare_first_page(self, sample_hashes: list[str],
                           bulk_hashes: list[str]) -> list[dict]:
        """
        Fast path: compare only the first page of the sample against every
        bulk page. Useful for single-page samples or quick scanning.
        """
        if not sample_hashes or not sample_hashes[0]:
            return []
        sh = sample_hashes[0]
        matches = []
        for i, bh in enumerate(bulk_hashes):
            if not bh:
                continue
            dist = _phash_distance(sh, bh)
            conf = _distance_to_confidence(dist)
            if conf >= self.threshold:
                borderline = 0.5 <= conf < 0.8
                matches.append({
                    "page_index": i,
                    "confidence": round(conf, 4),
                    "borderline": borderline,
                    "avg_hamming": float(dist),
                })
        return matches

    def find_boundaries(self, all_sample_matches: list[dict],
                        total_pages: int) -> list[dict]:
        """
        Derive split boundaries from sample match transitions.

        A boundary is placed between page i-1 and page i when:
        - page i matches a sample and page i-1 does NOT match the same sample
        - page i matches a DIFFERENT sample than page i-1

        Args:
            all_sample_matches: List of {page_index, sample_id, confidence, source}.
            total_pages: Total number of pages in the bulk PDF.

        Returns:
            List of boundary dicts: [{page, sample_id, confidence, source}, ...].
        """
        page_matches = {}  # page_index -> best match
        for m in all_sample_matches:
            pi = m["page_index"]
            if pi not in page_matches or m["confidence"] > page_matches[pi]["confidence"]:
                page_matches[pi] = m

        boundaries = []
        for i in range(1, total_pages):
            curr = page_matches.get(i)
            prev = page_matches.get(i - 1)

            if curr and (not prev or prev["sample_id"] != curr["sample_id"]):
                boundaries.append({
                    "page": i,
                    "sample_id": curr["sample_id"],
                    "confidence": curr["confidence"],
                    "source": curr.get("source", "phash"),
                })

        return boundaries

    @staticmethod
    def _deduplicate(matches: list[dict], window_size: int) -> list[dict]:
        """Keep only the highest-confidence match per window region."""
        if len(matches) <= 1:
            return matches
        matches.sort(key=lambda m: m["confidence"], reverse=True)
        kept = []
        used = set()
        for m in matches:
            pages = set(range(m["page_index"], m["page_index"] + window_size))
            if not pages.intersection(used):
                kept.append(m)
                used.update(pages)
        kept.sort(key=lambda m: m["page_index"])
        return kept


# =============================================================================
# LLMFallbackComparer
# =============================================================================


class LLMFallbackComparer:
    """
    Send borderline page pairs to an Ollama vision model for comparison.

    Prompt: "Are these two document pages from the same document type?
    Return ONLY valid JSON: {"same": true/false, "confidence": 0.0-1.0}"

    Args:
        ollama_host: Ollama API endpoint URL.
        model: Vision model name (e.g., "gemma4:e2b", "glm-ocr").
        timeout: HTTP request timeout in seconds.
    """

    PROMPT = (
        "Are these two document pages from the same document type?\n"
        "Consider layout, structure, headers, forms, and visual patterns.\n"
        "Return ONLY valid JSON: {\"same\": true/false, \"confidence\": 0.0-1.0}"
    )

    def __init__(self, ollama_host: str, model: str = "glm-ocr",
                 timeout: float = 60.0):
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def compare(self, page_a_path: str, page_b_path: str) -> Optional[dict]:  # noqa: E704
        """
        Compare two page images via Ollama vision model.

        Args:
            page_a_path: Path to first page PNG.
            page_b_path: Path to second page PNG.

        Returns:
            dict {"same": bool, "confidence": float}, or None on failure.
        """
        import base64

        try:
            with open(page_a_path, "rb") as f:
                img_a_b64 = base64.b64encode(f.read()).decode()
            with open(page_b_path, "rb") as f:
                img_b_b64 = base64.b64encode(f.read()).decode()
        except OSError as exc:
            logger.warning(f"LLM fallback: failed to read images: {exc}")
            return None

        payload = {
            "model": self.model,
            "prompt": self.PROMPT,
            "images": [img_a_b64, img_b_b64],
            "stream": False,
            "format": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.ollama_host}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
        except Exception as exc:
            logger.warning(f"LLM fallback: Ollama call failed: {exc}")
            return None

        try:
            data = resp.json()
            result = json.loads(data.get("response", "{}"))
            return {
                "same": bool(result.get("same", False)),
                "confidence": float(result.get("confidence", 0.0)),
            }
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning(f"LLM fallback: failed to parse response: {exc}")
            return None

    async def verify_borderline(self, matches: list[dict],
                                bulk_page_paths: list[Path],
                                sample_page_paths: dict) -> list[dict]:
        """
        Re-evaluate borderline matches using LLM vision comparison.

        Args:
            matches: List of match dicts from SimilarityEngine.
            bulk_page_paths: Ordered list of Paths to bulk page images.
            sample_page_paths: Dict mapping sample_id -> [Path of page images].

        Returns:
            Updated list of match dicts with source="llm" or source="llm_failed".
        """
        verified = []
        for m in matches:
            if not m.get("borderline"):
                m["source"] = "phash"
                verified.append(m)
                continue

            sid = m.get("sample_id")
            pi = m["page_index"]
            sample_paths = sample_page_paths.get(sid, [])
            if not sample_paths or pi >= len(bulk_page_paths):
                m["source"] = "phash"
                verified.append(m)
                continue

            result = await self.compare(
                str(sample_paths[0]),
                str(bulk_page_paths[pi]),
            )
            if result and result["same"]:
                m["confidence"] = round(result["confidence"], 4)
                m["source"] = "llm"
            else:
                m["source"] = "llm_failed"
                if result:
                    m["confidence"] = 0.0
            verified.append(m)

        return verified
