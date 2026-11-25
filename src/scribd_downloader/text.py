"""Render Scribd's native text with its original fonts and CSS geometry."""
import base64
from html import escape
from io import BytesIO
import re


# Used by minimal embeds without inline viewer CSS. Real embeds supply their
# own rules, including all wN spacers and lN kerning adjustments.
VIEWER_CSS = """
.newpage { white-space:nowrap; position:relative; color:#000; text-rendering:auto }
.text_layer { width:0; height:0; position:absolute; top:0; left:0;
              transform:scale(.2); transform-origin:top left }
.text_layer div,.text_layer span { white-space:nowrap; padding:0; margin:0;
                                 border:none; line-height:1 }
.text_layer span { height:1px }
.text_layer span.a,.text_layer span.g {
    position:absolute; left:0 }
"""


async def download_text_style(session, root, font_data, base_url, concurrency):
    # Keep the viewer rules rather than approximating its inline layout. Avoid
    # application stylesheets, scripts, tracking and lazy-loading machinery.
    styles = [node.text or "" for node in root.xpath("//style")
              if ".text_layer" in (node.text or "")]
    if not font_data:
        return "\n".join(styles)

    from .pages import _gather_cancel, _get, _image_url

    hosts, prefix = font_data.get("hosts"), font_data.get("asset_prefix")
    if (not isinstance(hosts, list) or not hosts or not isinstance(hosts[0], str)
            or not hosts[0] or not isinstance(prefix, str) or not prefix):
        raise ValueError("Document font metadata has no asset host or prefix")
    fonts = font_data["fonts"]
    font_rules = [""] * len(fonts)
    pending = iter(enumerate(fonts))

    async def worker():
        for index, font in pending:
            if not isinstance(font, dict):
                raise ValueError("Invalid embedded font declaration")
            family, weight, style, font_id = (font.get(key) for key in ("family", "weight", "style", "id"))
            if (not isinstance(family, str) or not re.fullmatch(r"ff[0-9]+", family)
                    or weight not in ("normal", "bold", "400", "700")
                    or style not in ("normal", "italic", "oblique")
                    or type(font_id) is not int or font_id < 0):
                raise ValueError(f"Unsupported embedded font declaration: {font!r}")
            url = _image_url(base_url, f"{hosts[0].rstrip('/')}/{prefix.strip('/')}/fonts/{font_id:04d}.woff2")
            contents, _ = await _get(session, url, base_url=base_url)
            encoded = base64.b64encode(contents).decode("ascii")
            font_rules[index] = (
                f"@font-face {{ font-family:{family}; font-weight:{weight}; font-style:{style};"
                f"src:url(data:font/woff2;base64,{encoded}) format('woff2'); }}\n"
                f"div.{family} span {{ font-family:{family}; font-weight:{weight}; font-style:{style}; }}"
            )

    await _gather_cancel(*(worker() for _ in range(min(concurrency, len(fonts)))))
    return "\n".join([*styles, *font_rules])


def text_document(pages, viewer_width, text_style):
    """Build a local, script-free print document; each source page has its own size."""
    rules = [VIEWER_CSS, text_style, """html,body { padding:0; margin:0 }
body { font-family:Arial,sans-serif; -webkit-print-color-adjust:exact; print-color-adjust:exact }
section { position:relative; overflow:hidden; break-after:page }
section:last-child { break-after:auto }
"""]
    sections = []
    for index, page in enumerate(pages):
        width, height = viewer_width, page.height * viewer_width / page.width
        rules.append(f"@page p{index} {{ size:{width:.9f}px {height:.9f}px; margin:0 }}")
        content = page.text_html
        if not content:
            # Programmatically constructed pages (e.g. offline benchmarks).
            spans = []
            for left, top, size, color, value in page.text_runs:
                if color is None:
                    continue
                rgb = ",".join(str(round(channel * 255)) for channel in color)
                spans.append(f'<span class="a" style="left:{left}px;top:{top}px;'
                             f'font-size:{size}px;color:rgb({rgb})">{escape(value)}</span>')
            content = '<div class="text_layer">' + "".join(spans) + '</div>'
        sections.append(
            f'<section style="page:p{index};width:{width:.9f}px;height:{height:.9f}px">'
            f'<div class="newpage" style="width:{page.width}px;height:{page.height}px;'
            f'transform:scale({viewer_width / page.width:.12f});transform-origin:top left">'
            f'{content}</div></section>'
        )
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'; font-src data:">'
            '<style>' + "\n".join(rules) + '</style></head><body>'
            + "".join(sections) + '</body></html>')


def render_text_pdf(pages, viewer_width, text_style, *, on_progress=None):
    from patchright.sync_api import sync_playwright
    import pikepdf

    contents = BytesIO()
    with pikepdf.Pdf.new() as result, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(java_script_enabled=False)
            tab = context.new_page()
            # All assets are embedded. Never fetch resources from document HTML.
            tab.route("**/*", lambda route: route.abort())
            tab.set_content(text_document(pages, viewer_width, text_style), wait_until="load")
            tab.emulate_media(media="screen")
            # Loading failure is fatal: falling back changes glyph widths and
            # can also expose codepoints meant only for a custom subset font.
            tab.evaluate("""async () => {
                const declared = new Set([...document.fonts].map(f => f.family));
                for (const node of document.querySelectorAll('.text_layer [class]')) {
                    for (const name of node.classList) {
                        if (/^ff\\d+$/.test(name) && !declared.has(name)) {
                            throw new Error('Missing embedded font: ' + name);
                        }
                    }
                }
                await Promise.all([...document.fonts].map(f => f.load()));
                await document.fonts.ready;
            }""")
            # Print each page in isolation. Chromium fragments unscaled absolute
            # descendants before applying scale(.2) in a multi-page flow, which
            # otherwise drops lines or adds blank pages. Keep the loaded fonts
            # and browser alive while switching the single visible section.
            for index, source in enumerate(pages):
                tab.evaluate("index => document.querySelectorAll('section').forEach((s,i) => "
                             "s.style.display = i === index ? 'block' : 'none')", index)
                data = tab.pdf(prefer_css_page_size=True, print_background=True)
                with pikepdf.Pdf.open(BytesIO(data)) as rendered:
                    if len(rendered.pages) != 1:
                        raise ValueError(f"Page {source.number}: text renderer produced extra pages")
                    result.pages.append(rendered.pages[0])
                if on_progress is not None:
                    on_progress("Assembling PDF", index + 1, len(pages))
            result.save(contents)
        finally:
            browser.close()
    contents.seek(0)
    return contents
