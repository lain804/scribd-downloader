"""Download Scribd page chunks and place their original images in a PDF."""
import asyncio
from dataclasses import dataclass
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urljoin, urlsplit

import img2pdf
from curl_cffi.requests import AsyncSession
from lxml import html
from PIL import Image
import pikepdf
from reportlab.pdfgen.canvas import Canvas

from .extractor import extract_embed_data


logger = logging.getLogger(__name__)
MAX_CONCURRENCY = 32
VIEWER_WIDTH = 1000
JSONP = re.compile(r"\s*(?:window\.)?page(\d+)_callback\s*\((\[.*\])\)\s*;?\s*", re.S)
CLIENT_CHALLENGE = b"<title>Client Challenge</title>"
INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _default_output_path(title: str) -> Path:
    filename = INVALID_FILENAME_CHARACTERS.sub("_", str(title or "")).strip().rstrip(".")
    # Common filesystems limit each filename to 255 bytes, including '.pdf'.
    filename = filename.encode("utf-8", "replace")[:240].decode("utf-8", "ignore").rstrip(" .")
    if not filename or filename in {".", ".."}:
        filename = "document"
    return Path.cwd() / f"{filename}.pdf"


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
    url = urljoin(base, value)
    # Scribd's browser viewer substitutes this CDN host.
    if url.startswith("http://html.scribd.com/"):
        url = url.replace("http://html.scribd.com/", "https://html.scribdassets.com/", 1)
    if urlsplit(url).scheme not in {"https", "http"}:
        raise ValueError(f"Unsupported page image URL: {url}")
    return url


def parse_page(root, number: int, base_url: str) -> Page:
    nodes = root.xpath(f"descendant-or-self::div[@id='page{number}']")
    if len(nodes) != 1:
        raise ValueError(f"Page {number}: expected one page div, found {len(nodes)}")
    page = nodes[0]
    css = _style(page)
    width, height = _pixels(css["width"]), _pixels(css["height"])
    if min(width, height) <= 0 or width * height > 40_000_000:
        raise ValueError(f"Page {number}: invalid or oversized canvas {width} x {height}")
    if css.get("transform", "none") != "none":
        raise ValueError(f"Page {number}: transformed page canvases are unsupported")
    # Pages can combine visible HTML text, image segments and transparent text
    # overlays. Retain text visibility rather than assuming images contain it.
    if page.xpath(".//canvas | .//svg"):
        raise ValueError(f"Page {number}: canvas/SVG page content is unsupported")
    text_runs = []
    for layer in page.xpath(
        ".//*[contains(concat(' ',normalize-space(@class),' '),' text_layer ')]"
    ):
        for span in layer.xpath(".//span[@style]"):
            style = _style(span)
            if "left" not in style or "top" not in style:
                continue
            text = "".join(span.itertext()).replace("\ufffd", "")
            if not text.strip():
                continue
            inherited = [style, *(_style(node) for node in span.iterancestors())]
            font_size = _pixels(next((css["font-size"] for css in inherited if css.get("font-size")), "0"))
            if font_size <= 0:
                continue
            color = _text_color(next((css["color"] for css in inherited if css.get("color")), None))
            if any(css.get("opacity") in {"0", "0.0", "0.00"} for css in inherited):
                color = None
            text_runs.append((
                _pixels(style["left"]), _pixels(style["top"]), font_size,
                color, text,
            ))
    placements = []
    for img in page.xpath(".//img[contains(concat(' ',normalize-space(@class),' '),' absimg ')]"):
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
    return Page(number, width, height, tuple(placements), tuple(text_runs))


def _text_pdf(pages: list[Page]) -> BytesIO:
    """Create small PDF pages containing the visible and searchable HTML text."""
    contents = BytesIO()
    overlay = Canvas(contents, pageCompression=1)
    for page in pages:
        width = VIEWER_WIDTH * 72 / 96
        height = page.height * width / page.width
        overlay.setPageSize((width, height))
        scale = width / page.width * 0.2  # Scribd text layers use transform: scale(.2).
        obj = overlay.beginText()
        previous_size = None
        previous_color = object()
        for left, top, font_size, color, text in page.text_runs:
            # Helvetica uses WinAnsi; replace unsupported glyphs while keeping
            # the rest of the page's searchable text.
            text = text.encode("cp1252", "replace").decode("cp1252")
            obj.setTextOrigin(left * scale, height - (top + font_size) * scale)
            if font_size != previous_size:
                obj.setFont("Helvetica", font_size * scale)
                previous_size = font_size
            if color != previous_color:
                obj.setTextRenderMode(3 if color is None else 0)
                if color is not None:
                    obj.setFillColorRGB(*color)
                previous_color = color
            obj.textOut(text)
        overlay.drawText(obj)
        overlay.showPage()
    overlay.save()
    contents.seek(0)
    return contents


def _assemble_pdf(pages: list[Page], assets: dict[str, Path], directory: Path, title: str) -> Path:
    """Embed each asset once, then reconstruct CSS clipping using PDF commands.

    img2pdf preserves JPEG/PNG compression, bit depth, color profiles and alpha.
    No page bitmap, upscaling, or lossy re-encoding is needed. pikepdf retains
    the encoded streams and shared image objects while replacing their pages.
    """
    urls = list(assets)
    staged = directory / "document.pdf"
    if not urls:
        with pikepdf.Pdf.open(_text_pdf(pages)) as document:
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
    with pikepdf.Pdf.open(native) as document, pikepdf.Pdf.open(_text_pdf(pages)) as text:
        images = {
            url: asset_page.Resources.XObject.Im0
            for url, asset_page in zip(urls, document.pages, strict=True)
        }
        for page, text_page in zip(pages, text.pages, strict=True):
            document.pages.append(text_page)
            pdf_page = document.pages[-1]
            resources = pikepdf.Dictionary()
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
                    names[placement.url] = f"/Im{len(names)}"
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


async def _get(session: AsyncSession, url: str, *, headers=None) -> bytes:
    response = await session.get(url, headers=headers)
    response.raise_for_status()
    return response.content


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
    session: AsyncSession, root, metadata, count: int, base_url: str,
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
            contents = await _get(session, chunk_url, headers={"Referer": base_url})
            add_page(number, _parse_jsonp(contents.decode("utf-8-sig"), number), chunk_url)

    await _gather_cancel(*(worker() for _ in range(min(concurrency, len(missing)))))
    return pages


def _validate_asset(contents: bytes, url: str) -> None:
    try:
        with Image.open(BytesIO(contents)) as image:
            # Check headers before any pixel decoding/allocation.
            if image.width * image.height > 40_000_000:
                raise ValueError("image exceeds 40 million pixels")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not read image {url}: {exc}") from exc


async def _download_assets(session, root, metadata, count, url, directory, concurrency):
    assets = {}
    pending = asyncio.Queue()
    image_headers = {
        "Referer": url,
        # PDF can embed JPEG/PNG directly. Negotiating WebP/AVIF forces a
        # decode and lossless transcode, often much larger than the source.
        "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
    }

    def enqueue(page):
        for placement in page.placements:
            if placement.url not in assets:
                assets[placement.url] = directory / f"asset-{len(assets):06d}.image"
                pending.put_nowait(placement.url)

    async def producer():
        pages = await _fetch_pages(
            session, root, metadata, count, url, concurrency=concurrency, on_page=enqueue,
        )
        for _ in range(concurrency):
            pending.put_nowait(None)
        return pages

    async def worker():
        while (asset_url := await pending.get()) is not None:
            contents = await _get(session, asset_url, headers=image_headers)
            _validate_asset(contents, asset_url)
            assets[asset_url].write_bytes(contents)

    results = await _gather_cancel(producer(), *(worker() for _ in range(concurrency)))
    return results[0], assets


async def _ensure_chromium() -> None:
    from patchright.async_api import async_playwright

    # Check the browser revision required by Patchright, rather than relying
    # on Scrapling's install marker or Playwright's potentially different revision.
    async with async_playwright() as playwright:
        if Path(playwright.chromium.executable_path).is_file():
            return
    logger.warning("Installing Chromium for Scribd's browser challenge; this may take a few minutes")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "patchright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode:
        raise RuntimeError(
            "Could not install Chromium for the browser challenge: "
            + output.decode("utf-8", errors="replace").strip()
        )


async def _pass_client_challenge(url: str, timeout: int) -> bytes:
    from scrapling.fetchers import AsyncStealthySession

    await _ensure_chromium()
    responses = []

    async def capture_navigation(page):
        def capture(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                responses.append(response)

        page.on("response", capture)

    async def wait_for_challenge(page):
        await page.wait_for_function(
            "document.title !== 'Client Challenge'",
            timeout=timeout * 1000,
        )

    async with AsyncStealthySession(
        headless=True, disable_resources=True, network_idle=False,
        timeout=timeout * 1000, max_pages=1, retries=1,
    ) as browser:
        await browser.fetch(
            url, page_setup=capture_navigation, page_action=wait_for_challenge,
            google_search=False,
        )
        if not responses:
            raise RuntimeError("Scribd's browser challenge returned no document response")
        response = responses[-1]
        # Use the original HTML: browser rendering mutates page dimensions and
        # transforms. Replaying clearance cookies through curl can be challenged
        # again because its browser fingerprint differs.
        contents = await response.body()
        if CLIENT_CHALLENGE in contents:
            raise RuntimeError("Scribd's browser challenge did not complete successfully")
        if response.status >= 400:
            raise RuntimeError(f"Scribd returned HTTP {response.status} after the browser challenge")
        return contents


async def download_pdf(
    url: str, output: Path | None = None, timeout: int = 30,
    *, concurrency: int = MAX_CONCURRENCY,
) -> int:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 256:
        raise ValueError("Concurrency must be an integer between 1 and 256")
    started = perf_counter()
    request_headers = {
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    }
    async with AsyncSession(
        impersonate="chrome", timeout=timeout, max_clients=concurrency, headers=request_headers,
    ) as session:
        embed_response = await session.get(url)
        embed_contents = embed_response.content
        if CLIENT_CHALLENGE in embed_contents:
            logger.info("Scribd requires a browser challenge; opening headless Chromium")
            embed_contents = await _pass_client_challenge(url, timeout)
        else:
            embed_response.raise_for_status()
        try:
            root = html.fromstring(embed_contents)
            options, metadata = extract_embed_data(root)
        except Exception as exc:
            raise RuntimeError(f"Could not read Scribd document metadata: {exc}") from exc
        document_title = str(options["document"].get("title") or "")
        output = Path(output) if output is not None else _default_output_path(document_title)
        output.parent.mkdir(parents=True, exist_ok=True)
        count = options["document"]["page_count"]
        with TemporaryDirectory(prefix="scribd-", dir=output.parent) as temporary:
            directory = Path(temporary)
            pages, assets = await _download_assets(
                session, root, metadata, count, url, directory, concurrency,
            )
            fetched = perf_counter()
            logger.info(
                "Fetched %d unique images for %d pages in %.2fs",
                len(assets), count, fetched - started,
            )
            staged = _assemble_pdf(pages, assets, directory, document_title)
            size = staged.stat().st_size
            staged.replace(output)
    logger.info(
        "Saved %s (%d pages, %.2f MiB); assembly %.2fs, total %.2fs",
        output, count, size / 1024**2, perf_counter() - fetched, perf_counter() - started,
    )
    return count
