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


def screenshots_differ(img_a: Optional[bytes], img_b: Optional[bytes], threshold: float = 0.02) -> bool:
    """
    Check if two screenshots are visually different.

    Args:
        img_a: First screenshot bytes (or None).
        img_b: Second screenshot bytes (or None).
        threshold: Minimum pixel diff ratio to consider as changed (default 2%).

    Returns:
        True if screenshots differ or either is None.
    """
    if img_a is None or img_b is None:
        return True
    return compute_pixel_diff(img_a, img_b) > threshold


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
