"""Download Scribd page chunks and place their original images in a PDF."""
import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from io import BytesIO
import json
import logging
import math
import ntpath
import os
from pathlib import Path
from random import uniform
import re
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic, perf_counter, time
from urllib.parse import urljoin, urlsplit

import img2pdf
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import HTTPError
from lxml import html
from PIL import Image
import pikepdf
from reportlab.pdfgen.canvas import Canvas

from .extractor import extract_embed_data


logger = logging.getLogger(__name__)
MAX_CONCURRENCY = 32
MAX_RATE_LIMIT_RETRIES = 3
MAX_RETRY_WAIT = 60
VIEWER_WIDTH = 1000
JSONP = re.compile(r"\s*(?:window\.)?page(\d+)_callback\s*\((\[.*\])\)\s*;?\s*", re.S)
CLIENT_CHALLENGE = b"<title>Client Challenge</title>"
MAX_EMBED_BYTES = 32 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
RASTER_FORMATS = ("AVIF", "BMP", "GIF", "ICO", "JPEG", "JPEG2000", "PNG", "TIFF", "WEBP")
INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
ProgressCallback = Callable[[str, int, int], None]


class _DownloadSession(AsyncSession):
    def __init__(self, timeout: int = 30, concurrency: int = MAX_CONCURRENCY):
        super().__init__(
            impersonate="chrome", timeout=timeout, max_clients=concurrency,
            headers={"Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8"},
        )
        self.slots = asyncio.Semaphore(concurrency)
        self.cooldowns: dict[tuple[str, str, int], float] = {}
        self.chromium_lock = asyncio.Lock()
        self.chromium_ready = False

    async def ensure_chromium(self):
        async with self.chromium_lock:
            if not self.chromium_ready:
                await _ensure_chromium()
                self.chromium_ready = True

    @asynccontextmanager
    async def request_slot(self, origin):
        while True:
            async with self.slots:
                delay = self.cooldowns.get(origin, 0) - monotonic()
                if delay <= 0:
                    yield
                    return
            if delay > MAX_RETRY_WAIT:
                raise HTTPError(f"{origin[1]} requested a cooldown longer than {MAX_RETRY_WAIT}s")
            # Spread queued requests out when a shared cooldown ends.
            await asyncio.sleep(delay + uniform(0, 0.25))


def _retry_after(value: str) -> float | None:
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0, date.timestamp() - time())
    except (ValueError, OverflowError, OSError):
        return None


def _default_output_path(title: str, directory: Path | None = None) -> Path:
    filename = INVALID_FILENAME_CHARACTERS.sub("_", str(title or "")).strip().rstrip(".")
    # Common filesystems limit each filename to 255 bytes, including '.pdf'.
    filename = filename.encode("utf-8", "replace")[:240].decode("utf-8", "ignore").rstrip(" .")
    if not filename or filename in {".", ".."}:
        filename = "document"
    if ntpath.isreserved(filename):
        filename = f"_{filename}"
    return (Path(directory) if directory is not None else Path.cwd()) / f"{filename}.pdf"


def _publish_new_pdf(staged: Path, output: Path) -> Path:
    # POSIX rename replaces existing files. A hard link fails if the name is taken.
    candidate = output
    number = 1
    while True:
        try:
            if sys.platform == "win32":
                staged.rename(candidate)
            else:
                os.link(staged, candidate)
            return candidate
        except FileExistsError:
            pass
        candidate = output.with_name(f"{output.stem} ({number}){output.suffix}")
        number += 1


@dataclass(frozen=True)
class Placement:
    url: str
    left: float
    top: float
    width: float
    height: float
    clip: tuple[float, float, float, float]  # top, right, bottom, left


@dataclass(frozen=True)
class Page:
    number: int
    width: float
    height: float
    placements: tuple[Placement, ...]
    text_runs: tuple[tuple[float, float, float, tuple[float, float, float] | None, str], ...]
    text_html: str = ""


def _style(node) -> dict[str, str]:
    return {
        key.strip().lower(): value.strip().lower()
        for item in node.get("style", "").split(";") if ":" in item
        for key, value in [item.split(":", 1)]
    }


def _pixels(value: str) -> float:
    match = re.fullmatch(r"(-?(?:\d+(?:\.\d*)?|\.\d+))(?:px)?", value.strip())
    if not match:
        raise ValueError(f"Unsupported CSS length: {value!r}")
    result = float(match.group(1))
    if not math.isfinite(result):
        raise ValueError(f"Invalid CSS length: {value!r}")
    return result


def _text_color(value: str | None) -> tuple[float, float, float] | None:
    if not value:
        return (0.0, 0.0, 0.0)
    value = value.strip().lower()
    if value == "transparent":
        return None
    if value == "white":
        return (1.0, 1.0, 1.0)
    if value == "black":
        return (0.0, 0.0, 0.0)
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        if len(digits) == 6:
            try:
                return tuple(int(digits[index:index + 2], 16) / 255 for index in (0, 2, 4))
            except ValueError:
                pass
    return (0.0, 0.0, 0.0)


def _image_url(base: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Scribd asset URL must be a string")
    url = urljoin(base, value)
    # Scribd's browser viewer substitutes this CDN host.
    if url.startswith("http://html.scribd.com/"):
        url = url.replace("http://html.scribd.com/", "https://html.scribdassets.com/", 1)
    scheme, host, port = _http_origin(url)
    if scheme == "http" and port == 80 and _trusted_origin(("https", host, 443)):
        url = urlsplit(url)._replace(scheme="https", netloc=host).geturl()
    return url


def _http_origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if (parts.scheme not in {"https", "http"} or not parts.hostname
            or parts.username is not None or "\\" in url
            or " " in parts.netloc or any(ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError(f"Invalid HTTP URL: {url!r}")
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname, port


def _trusted_origin(origin: tuple[str, str, int]) -> bool:
    scheme, host, port = origin
    return scheme == "https" and port == 443 and (
        host in {"scribd.com", "scribdassets.com"}
        or host.endswith((".scribd.com", ".scribdassets.com"))
    )


def _scan_ocr(style: dict[str, str]) -> bool:
    """Recognize Scribd's half-opacity OCR markup over scanned page images."""
    if style.get("filter") != "alpha(opacity=50)":
        return False
    try:
        return float(style.get("opacity", "1")) == 0.5
    except ValueError:
        return False


def parse_page(root, number: int, base_url: str) -> Page:
    nodes = root.xpath(f"descendant-or-self::div[@id='page{number}']")
    if len(nodes) != 1:
        raise ValueError(f"Page {number}: expected one page div, found {len(nodes)}")
    page = nodes[0]
    css = _style(page)
    width, height = _pixels(css["width"]), _pixels(css["height"])
    if (min(width, height) <= 0 or width * height > 40_000_000
            or not 3 <= height / width * (VIEWER_WIDTH * 72 / 96) <= 14_400):
        raise ValueError(f"Page {number}: invalid or oversized canvas {width} x {height}")
    if css.get("transform", "none") != "none":
        raise ValueError(f"Page {number}: transformed page canvases are unsupported")
    # Pages can combine visible HTML text and image segments. Ignore hidden
    # overlays rather than adding a searchable copy of the image content.
    if page.xpath(".//canvas | .//svg"):
        raise ValueError(f"Page {number}: canvas/SVG page content is unsupported")
    images = page.xpath(".//img[contains(concat(' ',normalize-space(@class),' '),' absimg ')]")
    text_runs = []
    layers = page.xpath(
        ".//*[contains(concat(' ',normalize-space(@class),' '),' text_layer ')]"
    )
    for layer in layers:
        for span in layer.xpath(".//span[@style]"):
            style = _style(span)
            if "left" not in style or "top" not in style:
                continue
            text = "".join(span.itertext())
            if not text.strip():
                continue
            inherited = [style, *(_style(node) for node in span.iterancestors())]
            font_size = _pixels(next((css["font-size"] for css in inherited if css.get("font-size")), "0"))
            if font_size <= 0:
                continue
            color = _text_color(next((css["color"] for css in inherited if css.get("color")), None))
            if color is None or any(css.get("opacity") in {"0", "0.0", "0.00"} for css in inherited):
                continue
            # OCR duplicates text already present in the scan. Omit it while
            # retaining ordinary visible HTML additions to the source page.
            if images and any(_scan_ocr(css) for css in inherited):
                continue
            text_runs.append((
                _pixels(style["left"]), _pixels(style["top"]), font_size,
                color, text,
            ))
    placements = []
    for img in images:
        style = _style(img)
        left, top = _pixels(style.get("left", "0")), _pixels(style.get("top", "0"))
        image_width, image_height = _pixels(style["width"]), _pixels(style["height"])
        if min(image_width, image_height) <= 0:
            raise ValueError(f"Page {number}: invalid image dimensions")
        clip = (0.0, image_width, image_height, 0.0)
        value = style.get("clip", "auto")
        if value != "auto":
            match = re.fullmatch(r"rect\((.*?)\)", value)
            if not match:
                raise ValueError(f"Page {number}: unsupported clip {value!r}")
            edges = [part.strip() for part in re.split(r"[ ,]+", match.group(1).strip())]
            if len(edges) != 4:
                raise ValueError(f"Page {number}: malformed clip {value!r}")
            top_edge, right_edge, bottom_edge, left_edge = (
                default if edge == "auto" else _pixels(edge)
                for edge, default in zip(edges, (0.0, image_width, image_height, 0.0))
            )
            clip = (top_edge, right_edge, bottom_edge, left_edge)
        value = img.get("orig") or img.get("src")
        if not value:
            raise ValueError(f"Page {number}: image has no URL")
        if style.get("transform", "none") != "none" or style.get("opacity", "1") != "1":
            raise ValueError(f"Page {number}: transformed or translucent image is unsupported")
        placements.append(Placement(_image_url(base_url, value), left, top, image_width, image_height, clip))
    if not placements and not text_runs:
        raise ValueError(f"Page {number}: no image segments or supported text were found")
    # Retain the inline spacer spans, kerning, font classes and Unicode exactly.
    # itertext() is useful for detecting visible content, not for rendering it.
    text_layers = []
    for layer in layers if text_runs else ():
        layer = deepcopy(layer)
        def hidden(node):
            style = _style(node)
            return (style.get("color") == "transparent"
                    or style.get("opacity") in {"0", "0.0", "0.00"}
                    or bool(images and _scan_ocr(style)))

        if hidden(layer):
            continue
        for node in list(layer.iter()):
            if node.getparent() is None:
                continue
            style = _style(node)
            if node.tag == "span" and "left" in style and "top" in style:
                classes = node.get("class", "").split()
                if not {"a", "g"}.intersection(classes):
                    node.set("class", " ".join([*classes, "a"]))
            if node.tag in {"script", "iframe", "object", "embed", "link", "img", "style",
                            "meta", "base", "form", "input", "button", "select", "textarea"}:
                node.drop_tree()
            elif hidden(node):
                node.drop_tree()
        text_layers.append(html.tostring(layer, encoding="unicode", with_tail=False))
    return Page(number, width, height, tuple(placements), tuple(text_runs), "".join(text_layers))


def _text_pdf(
    pages: list[Page], *, on_progress: ProgressCallback | None = None, text_style: str = "",
) -> BytesIO:
    """Create small PDF pages containing only the source's visible HTML text."""
    if any(page.text_html and page.text_runs for page in pages):
        from .text import render_text_pdf

        return render_text_pdf(pages, VIEWER_WIDTH, text_style, on_progress=on_progress)
    contents = BytesIO()
    overlay = Canvas(contents, pageCompression=1)
    for number, page in enumerate(pages, 1):
        width = VIEWER_WIDTH * 72 / 96
        height = page.height * width / page.width
        overlay.setPageSize((width, height))
        scale = width / page.width * 0.2  # Scribd text layers use transform: scale(.2).
        obj = overlay.beginText()
        previous_size = None
        previous_color = object()
        for left, top, font_size, color, text in page.text_runs:
            if color is None:
                continue
            # Helvetica uses WinAnsi; replace unsupported glyphs while keeping
            # the rest of the page's visible text.
            text = text.encode("cp1252", "replace").decode("cp1252")
            obj.setTextOrigin(left * scale, height - (top + font_size) * scale)
            if font_size != previous_size:
                obj.setFont("Helvetica", font_size * scale)
                previous_size = font_size
            if color != previous_color:
                obj.setFillColorRGB(*color)
                previous_color = color
            obj.textOut(text)
        overlay.drawText(obj)
        overlay.showPage()
        if on_progress is not None:
            on_progress("Assembling PDF", number, len(pages))
    overlay.save()
    contents.seek(0)
    return contents


def _assemble_pdf(
    pages: list[Page], assets: dict[str, Path], directory: Path, title: str,
    *, on_progress: ProgressCallback | None = None, destination: Path | None = None,
    text_style: str = "",
) -> Path:
    """Embed each asset once, then reconstruct CSS clipping using PDF commands.

    img2pdf preserves JPEG/PNG compression, bit depth, color profiles and alpha.
    No page bitmap, upscaling, or lossy re-encoding is needed. pikepdf retains
    the encoded streams and shared image objects while replacing their pages.
    """
    urls = list(assets)
    if on_progress is not None:
        on_progress("Assembling PDF", 0, len(pages))
    staged = destination if destination is not None else directory / "document.pdf"
    if not urls:
        with pikepdf.Pdf.open(_text_pdf(pages, on_progress=on_progress, text_style=text_style)) as document:
            document.docinfo["/Title"] = title
            document.save(staged, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return staged
    native = directory / "assets.pdf"
    with native.open("wb") as stream:
        img2pdf.convert(
            [str(assets[url]) for url in urls], outputstream=stream,
            engine=img2pdf.Engine.internal, rotation=img2pdf.Rotation.none,
            first_frame_only=True, title=title,
        )
    with pikepdf.Pdf.open(native) as document, pikepdf.Pdf.open(_text_pdf(pages, text_style=text_style)) as text:
        images = {
            url: asset_page.Resources.XObject.Im0
            for url, asset_page in zip(urls, document.pages, strict=True)
        }
        for number, (page, text_page) in enumerate(zip(pages, text.pages, strict=True), 1):
            document.pages.append(text_page)
            pdf_page = document.pages[-1]
            # Chromium can put text in Form XObjects (e.g. transformed layers).
            # Preserve these when adding the original image assets.
            resources = pikepdf.Dictionary(pdf_page.Resources.get("/XObject", {}))
            names = {}
            scale = VIEWER_WIDTH * 72 / 96 / page.width
            commands = [f"q {scale:.9f} 0 0 {scale:.9f} 0 0 cm"]
            for placement in page.placements:
                top, right, bottom, left = placement.clip
                # Intersect CSS clipping with the image and page boundaries.
                left = max(0, left, -placement.left)
                top = max(0, top, -placement.top)
                right = min(placement.width, right, page.width - placement.left)
                bottom = min(placement.height, bottom, page.height - placement.top)
                if right <= left or bottom <= top:
                    continue
                if placement.url not in names:
                    index = len(names)
                    while f"/Im{index}" in resources:
                        index += 1
                    names[placement.url] = f"/Im{index}"
                    resources[names[placement.url]] = images[placement.url]
                name = names[placement.url]
                commands.append(
                    f"q {placement.left + left:.6f} {page.height - placement.top - bottom:.6f} "
                    f"{right - left:.6f} {bottom - top:.6f} re W n "
                    f"{placement.width:.6f} 0 0 {placement.height:.6f} "
                    f"{placement.left:.6f} {page.height - placement.top - placement.height:.6f} cm "
                    f"{name} Do Q"
                )
            commands.append("Q")
            pdf_page.Resources.XObject = resources
            pdf_page.contents_add(pikepdf.Stream(document, "\n".join(commands).encode("ascii")), prepend=True)
            if on_progress is not None:
                on_progress("Assembling PDF", number, len(pages))
        del document.pages[:len(urls)]
        document.save(staged, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return staged


def _parse_jsonp(source: str, number: int):
    match = JSONP.fullmatch(source)
    if not match or int(match.group(1)) != number:
        raise ValueError(f"Page {number}: invalid JSONP response")
    payload = json.loads(match.group(2))
    if not payload or not isinstance(payload[0], str):
        raise ValueError(f"Page {number}: JSONP response has no page HTML")
    return html.fromstring(payload[0])


async def _get(
    session: _DownloadSession, url: str, *, base_url: str, headers=None,
    limit: int = 16 * 1024 * 1024, allow_challenge: bool = False, destination: Path | None = None,
) -> tuple[bytes, str]:
    origin = _http_origin(base_url)
    headers = {"Referer": base_url, **(headers or {})}
    redirects = retries = 0
    while True:
        target = _http_origin(url)
        if target != origin and not _trusted_origin(target):
            raise ValueError(f"Untrusted Scribd asset or redirect URL: {url!r}")
        async with session.request_slot(target):
            with (destination.open("wb") if destination is not None else BytesIO()) as body:
                received = 0

                def receive(chunk):
                    nonlocal received
                    received += len(chunk)
                    if received > limit:
                        raise ValueError(f"Scribd response exceeds {limit:,} bytes: {url!r}")
                    return body.write(chunk)

                response = await session.get(url, headers=headers, allow_redirects=False,
                                             content_callback=receive)
                retry_after = response.headers.get("Retry-After", "")
                delay = _retry_after(retry_after) if retry_after else None
                if response.status_code == 429 or (response.status_code == 503 and delay is not None):
                    if delay is None:
                        delay = 0.5 * 2**retries + uniform(0, 0.25)
                    session.cooldowns[target] = max(session.cooldowns.get(target, 0), monotonic() + delay)
                    if retries == MAX_RATE_LIMIT_RETRIES or delay > MAX_RETRY_WAIT:
                        response.raise_for_status()
                    retries += 1
                    logger.info("%s returned HTTP %d, retrying in %.2fs", target[1], response.status_code, delay)
                    continue
                location = response.headers.get("Location")
                if response.status_code in {301, 302, 303, 307, 308} and location:
                    if redirects == 5:
                        raise ValueError("Too many redirects while fetching Scribd content")
                    redirects += 1
                    url = _image_url(response.url, location)
                    continue
                contents = body.getvalue() if isinstance(body, BytesIO) else b""
                if not (allow_challenge and CLIENT_CHALLENGE in contents):
                    response.raise_for_status()
                return contents, response.url


async def _gather_cancel(*coroutines):
    """Propagate the original error and stop sibling requests before closing curl."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _fetch_pages(
    session: _DownloadSession, root, metadata, count: int, base_url: str,
    *, concurrency: int = MAX_CONCURRENCY, on_page=None,
) -> list[Page]:
    by_number = {}
    for entry in metadata:
        if not isinstance(entry, dict):
            raise ValueError("Invalid page metadata")
        number = entry.get("pageNum")
        if isinstance(number, int) and not isinstance(number, bool):
            if number in by_number:
                raise ValueError(f"Duplicate metadata for page {number}")
            by_number[number] = entry
    inline_pages = {}
    for node in root.xpath("//div[starts-with(@id,'page')]"):
        match = re.fullmatch(r"page(\d+)", node.get("id", ""))
        if match:
            number = int(match.group(1))
            if number in inline_pages:
                raise ValueError(f"Duplicate inline data for page {number}")
            inline_pages[number] = node
    missing = []
    pages = [None] * count

    def add_page(number, source, base):
        page = parse_page(source, number, base)
        pages[number - 1] = page
        if on_page is not None:
            on_page(page)

    for number in range(1, count + 1):
        entry = by_number.get(number, {})
        if entry.get("blur"):
            raise ValueError(f"Page {number} is blurred or unavailable")
        chunk_url = entry.get("contentUrl")
        base = _image_url(base_url, chunk_url) if chunk_url else base_url
        if number in inline_pages:
            add_page(number, inline_pages[number], base)
        elif chunk_url:
            missing.append((number, base))
        else:
            raise ValueError(f"Page {number}: no inline data or page URL")
    # A fixed worker count avoids creating one asyncio task per document page.
    pending = iter(missing)

    async def worker():
        for number, chunk_url in pending:
            contents, effective_url = await _get(session, chunk_url, base_url=base_url)
            add_page(number, _parse_jsonp(contents.decode("utf-8-sig"), number), effective_url)

    await _gather_cancel(*(worker() for _ in range(min(concurrency, len(missing)))))
    return pages


def _validate_asset(path: Path, url: str) -> None:
    try:
        # Pillow can also open EPS, which invokes Ghostscript when decoded.
        with Image.open(path, formats=RASTER_FORMATS) as image:
            # Check headers before any pixel decoding/allocation.
            if image.width * image.height > 40_000_000:
                raise ValueError("image exceeds 40 million pixels")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not read image {url}: {exc}") from exc


async def _download_assets(
    session, root, metadata, count, url, directory, concurrency,
    *, on_progress: ProgressCallback | None = None, on_page: Callable[[Page], None] | None = None,
):
    assets = {}
    pending = asyncio.Queue()
    downloaded = set()
    waiting = {}
    remaining = {}
    completed = 0
    if on_progress is not None:
        on_progress("Downloading pages", 0, count)
    image_headers = {
        # PDF can embed JPEG/PNG directly. Negotiating WebP/AVIF forces a
        # decode and lossless transcode, often much larger than the source.
        "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
    }

    def enqueue(page):
        nonlocal completed
        if on_page is not None:
            on_page(page)
        for placement in page.placements:
            if placement.url not in assets:
                assets[placement.url] = directory / f"asset-{len(assets):06d}.image"
                pending.put_nowait(placement.url)
        if on_progress is not None:
            needed = {placement.url for placement in page.placements} - downloaded
            if not needed:
                completed += 1
                on_progress("Downloading pages", completed, count)
            else:
                remaining[page.number] = len(needed)
                for asset_url in needed:
                    waiting.setdefault(asset_url, []).append(page.number)

    async def producer():
        pages = await _fetch_pages(
            session, root, metadata, count, url, concurrency=concurrency, on_page=enqueue,
        )
        for _ in range(concurrency):
            pending.put_nowait(None)
        return pages

    async def worker():
        nonlocal completed
        while (asset_url := await pending.get()) is not None:
            asset = assets[asset_url]
            await _get(session, asset_url, base_url=url, headers=image_headers,
                       limit=MAX_ASSET_BYTES, destination=asset)
            _validate_asset(asset, asset_url)
            if on_progress is not None:
                downloaded.add(asset_url)
                before = completed
                for number in waiting.pop(asset_url, ()):
                    remaining[number] -= 1
                    if remaining[number] == 0:
                        del remaining[number]
                        completed += 1
                if completed != before:
                    on_progress("Downloading pages", completed, count)

    results = await _gather_cancel(producer(), *(worker() for _ in range(concurrency)))
    return results[0], assets


async def _ensure_chromium() -> None:
    from patchright.async_api import async_playwright

    # Check the browser revision required by Patchright, rather than relying
    # on Scrapling's install marker or Playwright's potentially different revision.
    async with async_playwright() as playwright:
        if Path(playwright.chromium.executable_path).is_file():
            return
    logger.warning("Installing Chromium for Scribd text rendering and browser challenges; this may take a few minutes")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "patchright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode:
        raise RuntimeError(
            "Could not install Chromium: "
            + output.decode("utf-8", errors="replace").strip()
        )


async def _pass_client_challenge(url: str, timeout: int) -> bytes:
    from patchright.async_api import TimeoutError as BrowserTimeoutError
    from scrapling.fetchers import AsyncStealthySession

    async with AsyncStealthySession(
        headless=True, timeout=timeout * 1000, additional_args={"chromium_sandbox": True},
    ) as browser:
        page = await browser.context.new_page()
        responses = asyncio.Queue()

        def capture(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                responses.put_nowait(response)

        page.on("response", capture)
        try:
            async with asyncio.timeout(timeout):
                # Loading the full page can stall on unrelated resources. The
                # challenge also forbids the eval used by wait_for_function.
                # Watch original navigation bodies instead of the rendered DOM.
                await page.goto(url, wait_until="commit", timeout=timeout * 1000)
                while True:
                    response = await responses.get()
                    contents = await response.body()
                    if response.status >= 400:
                        raise RuntimeError(f"Scribd returned HTTP {response.status} after the browser challenge")
                    if CLIENT_CHALLENGE not in contents:
                        return contents
        except (TimeoutError, BrowserTimeoutError) as exc:
            raise RuntimeError("Scribd's browser challenge did not complete successfully before the timeout") from exc


async def download_pdf(
    url: str, output: Path | None = None, timeout: int = 30,
    *, concurrency: int = MAX_CONCURRENCY,
    output_dir: Path | None = None, overwrite: bool = True,
    on_progress: ProgressCallback | None = None,
) -> int:
    if output is not None and output_dir is not None:
        raise ValueError("Choose either an output PDF path or an output directory")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 256:
        raise ValueError("Concurrency must be an integer between 1 and 256")
    async with _DownloadSession(timeout, concurrency) as session:
        return await _download_pdf(
            session, url, output, output_dir=output_dir, overwrite=overwrite, on_progress=on_progress,
        )


async def _download_pdf(
    session: _DownloadSession, url: str, output: Path | None = None,
    *, output_dir: Path | None = None, overwrite: bool = True,
    on_progress: ProgressCallback | None = None,
) -> int:
    started = perf_counter()
    concurrency = session.max_clients
    embed_contents, url = await _get(session, url, base_url=url, limit=MAX_EMBED_BYTES,
                                     allow_challenge=True)
    if CLIENT_CHALLENGE in embed_contents:
        logger.info("Scribd requires a browser challenge; opening headless Chromium")
        await session.ensure_chromium()
        embed_contents = await _pass_client_challenge(url, session.timeout)
        if len(embed_contents) > MAX_EMBED_BYTES:
            raise ValueError(f"Scribd response exceeds {MAX_EMBED_BYTES:,} bytes")
    try:
        root = html.fromstring(embed_contents.decode("utf-8-sig"))
        options, metadata = extract_embed_data(root)
    except Exception as exc:
        raise RuntimeError(f"Could not read Scribd document metadata: {exc}") from exc
    document_title = str(options["document"].get("title") or "")
    output = Path(output) if output is not None else _default_output_path(document_title, output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = options["document"]["page_count"]
    temporary_parent = None if sys.platform == "win32" else output.parent
    with TemporaryDirectory(prefix="scribd-", dir=temporary_parent) as temporary:
        directory = Path(temporary)
        needs_text = asyncio.get_running_loop().create_future()

        def detect_text(page):
            if page.text_html and page.text_runs and not needs_text.done():
                needs_text.set_result(True)

        async def fetch_assets():
            document = await _download_assets(
                session, root, metadata, count, url, directory, concurrency,
                on_progress=on_progress, on_page=detect_text,
            )
            if not needs_text.done():
                needs_text.set_result(False)
            return document

        async def fetch_styles():
            if not await needs_text:
                return ""
            from .text import download_text_style

            return await download_text_style(session, root, options.get("font_data"), url, concurrency)

        (pages, assets), text_style = await _gather_cancel(fetch_assets(), fetch_styles())
        if needs_text.result():
            await session.ensure_chromium()
        fetched = perf_counter()
        logger.info(
            "Fetched %d unique images for %d pages in %.2fs",
            len(assets), count, fetched - started,
        )
        staged = directory / "document.pdf"
        if sys.platform == "win32":
            # Python's private temp directory has an owner-only Windows
            # ACL that survives moving files out. Stage the final PDF next
            # to its destination so it inherits normal folder permissions.
            with NamedTemporaryFile(prefix="scribd-", suffix=".pdf", dir=output.parent,
                                    delete=False) as stream:
                staged = Path(stream.name)
        try:
            assembly = asyncio.create_task(asyncio.to_thread(
                _assemble_pdf, pages, assets, directory, document_title,
                on_progress=on_progress, destination=staged, text_style=text_style,
            ))
            try:
                await asyncio.shield(assembly)
            except asyncio.CancelledError:
                # The assembly thread still owns the input and staging files.
                while not assembly.done():
                    try:
                        await asyncio.shield(assembly)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not assembly.cancelled():
                    assembly.exception()
                raise
            size = staged.stat().st_size
            output = staged.replace(output) if overwrite else _publish_new_pdf(staged, output)
        finally:
            staged.unlink(missing_ok=True)
        if on_progress is not None:
            on_progress("Saved PDF", count, count)
    logger.info(
        "Saved %s (%d pages, %.2f MiB); assembly %.2fs, total %.2fs",
        output, count, size / 1024**2, perf_counter() - fetched, perf_counter() - started,
    )
    return count
