"""
Lightweight screenshot comparison utilities for browser action verification.
Uses Pillow for pixel-level diff without LLM calls.
"""
from __future__ import annotations

import hashlib
import io
from collections import deque
from typing import Optional

from PIL import Image


def compute_pixel_diff(img_a: bytes, img_b: bytes, downsample: int = 4) -> float:
    """
    Compute the fraction of pixels that differ between two screenshots.

    Args:
        img_a: First screenshot (JPEG/PNG bytes).
        img_b: Second screenshot (JPEG/PNG bytes).
        downsample: Resize factor to speed up comparison (4 = 1/4 resolution).

    Returns:
        Float 0.0-1.0 representing the fraction of changed pixels.
        Returns 1.0 if either image cannot be decoded.
    """
    try:
        a = Image.open(io.BytesIO(img_a)).convert("L")
        b = Image.open(io.BytesIO(img_b)).convert("L")
    except Exception:
        return 1.0

    # Downsample for speed
    if downsample > 1:
        w, h = a.size
        small = (max(w // downsample, 1), max(h // downsample, 1))
        a = a.resize(small, Image.NEAREST)
        b = b.resize(small, Image.NEAREST)

    # Ensure same size
    if a.size != b.size:
        b = b.resize(a.size, Image.NEAREST)

    pixels_a = a.tobytes()
    pixels_b = b.tobytes()
    total = len(pixels_a)
    if total == 0:
        return 1.0

    # Count pixels with significant difference (threshold=20 to ignore compression artifacts)
    diff_count = sum(1 for pa, pb in zip(pixels_a, pixels_b) if abs(pa - pb) > 20)
    return diff_count / total


def compute_pixel_diff_roi(
    img_a: bytes,
    img_b: bytes,
    exclude_top_frac: float = 0.05,
    exclude_bottom_frac: float = 0.05,
    downsample: int = 4,
) -> float:
    """
    Compute pixel diff while masking out top/bottom regions prone to noise
    (navigation bar, cookie banners, status bars).

    Args:
        img_a: First screenshot (JPEG/PNG bytes).
        img_b: Second screenshot (JPEG/PNG bytes).
        exclude_top_frac: Fraction of image height to exclude from top (0.0-0.5).
        exclude_bottom_frac: Fraction of image height to exclude from bottom (0.0-0.5).
        downsample: Resize factor to speed up comparison.

    Returns:
        Float 0.0-1.0 representing the fraction of changed pixels in the ROI.
        Returns 1.0 if either image cannot be decoded.
    """
    try:
        a = Image.open(io.BytesIO(img_a)).convert("L")
        b = Image.open(io.BytesIO(img_b)).convert("L")
    except Exception:
        return 1.0

    if downsample > 1:
        w, h = a.size
        small = (max(w // downsample, 1), max(h // downsample, 1))
        a = a.resize(small, Image.NEAREST)
        b = b.resize(small, Image.NEAREST)

    if a.size != b.size:
        b = b.resize(a.size, Image.NEAREST)

    w, h = a.size
    # Proportional exclusion zones — works across all viewport sizes
    roi_top = max(0, int(h * exclude_top_frac))
    roi_bottom = max(roi_top + 1, h - int(h * exclude_bottom_frac))

    pixels_a = list(a.getdata())
    pixels_b = list(b.getdata())
    total = w * (roi_bottom - roi_top)
    if total <= 0:
        return compute_pixel_diff(img_a, img_b, downsample)

    diff_count = sum(
        1
        for row in range(roi_top, roi_bottom)
        for col in range(w)
        if abs(pixels_a[row * w + col] - pixels_b[row * w + col]) > 20
    )
    return diff_count / total


def compute_block_diff(img_a: bytes, img_b: bytes, block_size: int = 16) -> float:
    """
    Divide images into blocks and compare per-block mean values.

    More robust to noise (cursor blinks, compression artefacts) than pixel diff.

    Args:
        img_a: First screenshot (JPEG/PNG bytes).
        img_b: Second screenshot (JPEG/PNG bytes).
        block_size: Side length in pixels of each comparison block.

    Returns:
        Fraction of blocks (0.0-1.0) where mean grey value differs by > 10.
        Returns 1.0 if either image cannot be decoded.
    """
    try:
        a = Image.open(io.BytesIO(img_a)).convert("L")
        b = Image.open(io.BytesIO(img_b)).convert("L")
    except Exception:
        return 1.0

    # Downsample to at most 160px wide for speed
    max_w = 160
    w, h = a.size
    if w > max_w:
        scale = max_w / w
        a = a.resize((max_w, max(int(h * scale), 1)), Image.NEAREST)
        b = b.resize((max_w, max(int(h * scale), 1)), Image.NEAREST)

    if a.size != b.size:
        b = b.resize(a.size, Image.NEAREST)

    w, h = a.size
    pa = list(a.getdata())
    pb = list(b.getdata())

    changed = 0
    total = 0
    for by in range(0, h, block_size):
        for bx in range(0, w, block_size):
            pixels_a_block = [
                pa[row * w + col]
                for row in range(by, min(by + block_size, h))
                for col in range(bx, min(bx + block_size, w))
            ]
            pixels_b_block = [
                pb[row * w + col]
                for row in range(by, min(by + block_size, h))
                for col in range(bx, min(bx + block_size, w))
            ]
            if not pixels_a_block:
                continue
            mean_a = sum(pixels_a_block) / len(pixels_a_block)
            mean_b = sum(pixels_b_block) / len(pixels_b_block)
            total += 1
            if abs(mean_a - mean_b) > 10:
                changed += 1

    if total == 0:
        return 1.0
    return changed / total


def screenshots_differ(
    img_a: Optional[bytes],
    img_b: Optional[bytes],
    threshold: float = None,
    use_block_diff: bool = True,
) -> bool:
    """
    Layered check for meaningful visual change between two screenshots.

    Strategy:
    1. ROI pixel diff > threshold → True  (large-area change: page navigation, full-page update)
    2. Block diff > 8% → True             (structural change: modal appears, content loads)
    3. Otherwise → False                  (noise: cursor blink, ad rotation, GIF frame)

    Args:
        img_a: First screenshot bytes (or None).
        img_b: Second screenshot bytes (or None).
        threshold: Pixel diff threshold (defaults to settings.VISION_PIXEL_DIFF_THRESHOLD).
        use_block_diff: Whether to apply block-level comparison as a second pass.

    Returns:
        True if screenshots differ meaningfully or either is None.
    """
    if img_a is None or img_b is None:
        return True

    from config.settings import settings
    effective_threshold = threshold if threshold is not None else settings.VISION_PIXEL_DIFF_THRESHOLD

    if compute_pixel_diff_roi(img_a, img_b) > effective_threshold:
        return True

    if use_block_diff and compute_block_diff(img_a, img_b) > settings.VISION_BLOCK_DIFF_THRESHOLD:
        return True

    return False


def screenshots_meaningfully_differ(img_a: Optional[bytes], img_b: Optional[bytes]) -> bool:
    """
    Strict comparison for action-effect verification.

    Uses a higher pixel threshold (0.08) and block diff to avoid false positives
    from input-cursor flicker and minor layout shifts.
    """
    return screenshots_differ(img_a, img_b, threshold=0.08, use_block_diff=True)


def perceptual_hash(img_bytes: bytes, hash_size: int = 8) -> str:
    """
    Compute a simple perceptual hash for an image.
    Images that look similar produce the same hash.

    Args:
        img_bytes: Screenshot bytes.
        hash_size: Hash grid size (8 = 64-bit hash).

    Returns:
        Hex string hash, or empty string on failure.
    """
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
    except Exception:
        return ""

    img = img.resize((hash_size, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return hex(int(bits, 2))[2:].zfill(hash_size * hash_size // 4)


class VisualProgressTracker:
    """Track visual progress across browser steps using perceptual hashes.

    If the last ``window_size`` screenshots all share the same hash,
    the page is considered visually stuck.
    """

    def __init__(self, window_size: int = 3):
        self.window_size = window_size
        self._recent_hashes: deque[str] = deque(maxlen=window_size)

    def record(self, screenshot: bytes) -> bool:
        """Record a screenshot and return whether visual progress is being made.

        Returns False when the last ``window_size`` screenshots are identical
        (visually stuck). Returns True otherwise.
        """
        img_hash = perceptual_hash(screenshot)
        if not img_hash:
            # Cannot compute hash — assume progress to avoid false positives
            return True
        is_stuck = (
            len(self._recent_hashes) >= self.window_size
            and all(h == img_hash for h in self._recent_hashes)
        )
        self._recent_hashes.append(img_hash)
        return not is_stuck

    def reset(self) -> None:
        self._recent_hashes.clear()
