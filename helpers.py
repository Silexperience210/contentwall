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


# ---------------------------------------------------------------------------
# Range header parsing for audio/video streaming
# ---------------------------------------------------------------------------


def parse_range_header(range_header: str, file_size: int) -> Optional[tuple[int, int]]:
    """
    Parse a HTTP Range header like 'bytes=0-1023' and clamp to the file size.
    Returns (start, end_inclusive) or None if malformed / non-bytes / unsatisfiable.

    Only the first range is honored (multi-range responses are out of scope).
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    try:
        spec = range_header[len("bytes="):].split(",")[0].strip()
        if "-" not in spec:
            return None
        a, b = spec.split("-", 1)
        if a == "" and b:
            # "bytes=-N" = last N bytes
            length = int(b)
            start = max(0, file_size - length)
            end = file_size - 1
        elif a and b == "":
            # "bytes=N-" = from N to end
            start = int(a)
            end = file_size - 1
        else:
            start = int(a)
            end = int(b)
        if start < 0 or start >= file_size:
            return None
        end = min(end, file_size - 1)
        if end < start:
            return None
        return start, end
    except (ValueError, AttributeError):
        return None


def iter_file_range(path: str, start: int, end: int, chunk: int = 65536):
    """Yield bytes in [start, end] inclusive from a file."""
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


# ---------------------------------------------------------------------------
# Markdown rendering (best-effort, server-side)
# ---------------------------------------------------------------------------


def render_markdown_safe(text: str) -> str:
    """
    Render markdown to HTML using mistune if available, else escape and wrap.
    Includes a basic XSS sanitizer that strips raw <script>/<style>/<iframe>.
    Falls back to pre-formatted plain text if no markdown lib is installed.
    """
    if not text:
        return ""
    try:
        import mistune
        md = mistune.create_markdown(escape=True, hard_wrap=True)
        html = md(text)
    except Exception:
        # Plain fallback: escape and preserve line breaks
        import html as _html
        return f'<div class="cw-plain">{_html.escape(text)}</div>'

    # Strip dangerous tags defensively. mistune with escape=True already
    # escapes raw HTML, but we add a belt-and-suspenders pass.
    import re as _re
    for bad in ("script", "style", "iframe", "object", "embed"):
        html = _re.sub(
            rf"<{bad}.*?>.*?</{bad}>", "", html, flags=_re.IGNORECASE | _re.DOTALL
        )
        html = _re.sub(rf"<{bad}[^>]*/?>", "", html, flags=_re.IGNORECASE)
    # Strip event handlers like onclick=
    html = _re.sub(r"\son\w+=\"[^\"]*\"", "", html, flags=_re.IGNORECASE)
    html = _re.sub(r"\son\w+='[^']*'", "", html, flags=_re.IGNORECASE)
    html = _re.sub(r"javascript:", "", html, flags=_re.IGNORECASE)
    return html


# ---------------------------------------------------------------------------
# In-memory rate limiter (per-IP, fixed window)
# ---------------------------------------------------------------------------


from collections import deque  # noqa: E402
from threading import Lock     # noqa: E402

_rate_state: dict[str, deque] = {}
_rate_lock = Lock()


def rate_limit_check(
    key: str, max_requests: int = 10, window_seconds: int = 60
) -> bool:
    """
    Return True if the call is allowed, False if rate-limited.

    Stores a deque of recent timestamps per key. Old entries are popped on
    every call so memory stays bounded. This is per-process and per-instance
    — fine for a single-server LNbits node, which is the common deployment.
    """
    import time
    now = time.time()
    cutoff = now - window_seconds
    with _rate_lock:
        q = _rate_state.setdefault(key, deque())
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= max_requests:
            return False
        q.append(now)
        return True


# ---------------------------------------------------------------------------
# OG / Twitter / Nostr meta tag builder
# ---------------------------------------------------------------------------


def build_share_meta(title: str, description: str, page_url: str,
                     preview_image_url: Optional[str] = None) -> dict:
    """
    Returns a dict consumed by display.html to render Open Graph, Twitter
    Card and Nostr-ish meta tags. Keep titles ≤60 chars and descriptions
    ≤200 to play nice with Twitter / Nostr clients.
    """
    safe_title = (title or "Content")[:60]
    safe_desc = (description or "")[:200]
    return {
        "title": safe_title,
        "description": safe_desc,
        "url": page_url,
        "image": preview_image_url or "",
    }
