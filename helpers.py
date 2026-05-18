"""
Stateless helpers for ContentWall:
    * LNURL-pay encoding
    * Image watermarking (best-effort, no hard PIL dep)
    * Outbound webhook delivery
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from io import BytesIO
from typing import Optional

from loguru import logger

try:
    import bech32  # provided by lnbits' dependency tree
    _HAS_BECH32 = True
except Exception:
    _HAS_BECH32 = False


def encode_lnurl(url: str) -> str:
    """Encode an HTTPS endpoint as a bech32 LNURL string (LUD-01)."""
    if not _HAS_BECH32:
        return ""
    data = bech32.convertbits(url.encode("utf-8"), 8, 5, True)
    if data is None:
        return ""
    return bech32.bech32_encode("lnurl", data).upper()


def metadata_for_lnurl(title: str, description: str) -> str:
    """LUD-06 metadata JSON, hashed and bound to the BOLT11 description_hash."""
    blob = [["text/plain", description or title]]
    if title:
        blob.append(["text/long-desc", title])
    return json.dumps(blob)


def metadata_hash(metadata: str) -> bytes:
    return hashlib.sha256(metadata.encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# Watermarking
# ---------------------------------------------------------------------------


def watermark_image_bytes(
    src_path: str, payment_hash: str
) -> Optional[tuple[bytes, str]]:
    """
    Returns (bytes, content_type) of a watermarked copy or None if Pillow
    isn't available. The watermark is a subtle text overlay in the bottom-
    right showing 8 hex characters derived from the payment_hash, so a
    leaked image can be traced back to a specific buyer.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    try:
        img = Image.open(src_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        tag = f"⚡ {payment_hash[:8]}"
        # Font: try a few common ones, fall back to default bitmap font.
        font = None
        for candidate in (
            "DejaVuSans.ttf",
            "Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ):
            try:
                size = max(12, min(img.size) // 40)
                font = ImageFont.truetype(candidate, size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        # Bottom-right padding
        text_bbox = draw.textbbox((0, 0), tag, font=font)
        tw = text_bbox[2] - text_bbox[0]
        th = text_bbox[3] - text_bbox[1]
        margin = max(10, min(img.size) // 80)
        x = img.size[0] - tw - margin
        y = img.size[1] - th - margin * 2

        # Translucent dark backplate for legibility
        draw.rectangle(
            [x - 6, y - 4, x + tw + 6, y + th + 6],
            fill=(0, 0, 0, 110),
        )
        draw.text((x, y), tag, font=font, fill=(255, 165, 0, 220))

        out = Image.alpha_composite(img, overlay).convert("RGB")
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning(f"watermark_image_bytes failed: {exc}")
        return None


def make_blurred_preview(src_path: str) -> Optional[bytes]:
    """
    Server-side blurred preview (small, returned as JPEG bytes).
    Never includes the original image. Best-effort: returns None if no PIL.
    """
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return None

    try:
        img = Image.open(src_path).convert("RGB")
        # Downscale aggressively first so the client can't unblur back to original
        max_dim = 480
        ratio = max_dim / max(img.size)
        if ratio < 1:
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        img = img.filter(ImageFilter.GaussianBlur(radius=18))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception as exc:
        logger.warning(f"make_blurred_preview failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def fire_webhook(url: str, payload: dict) -> None:
    """
    POST a JSON payload to a buyer-configured URL, fire-and-forget.
    Failures are logged but never raised so they can't break the payment flow.
    """
    if not url:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.warning(f"webhook to {url} failed: {exc}")


def fire_and_forget_webhook(url: str, payload: dict) -> None:
    """Schedule fire_webhook without awaiting."""
    if not url:
        return
    try:
        asyncio.create_task(fire_webhook(url, payload))
    except RuntimeError:
        # No running loop — caller is outside async context; drop silently
        pass


# ---------------------------------------------------------------------------
# Teaser fallback for articles
# ---------------------------------------------------------------------------


def article_teaser(article_text: str, length: int = 280) -> str:
    """Pick the first N characters from an article on a word boundary."""
    if not article_text:
        return ""
    if len(article_text) <= length:
        return article_text
    cut = article_text[:length]
    # Cut on the last whitespace so we don't slice mid-word
    last_space = cut.rfind(" ")
    if last_space > length // 2:
        cut = cut[:last_space]
    return cut + "…"
