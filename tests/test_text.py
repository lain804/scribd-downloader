import asyncio
import base64
from pathlib import Path

from lxml import html
from pypdf import PdfReader
import pytest
import reportlab

from scribd_downloader import pages
from scribd_downloader.extractor import extract_embed_data
from scribd_downloader.text import download_text_style, render_text_pdf


def test_font_metadata_is_extracted_with_original_weight_and_style():
    root = html.fromstring('''<script>
        var docManager = new DocumentManager("scroll", false, {hasWoffFonts:true});
        docManager.fontAggregatorHosts = ["https://html.scribdassets.com"];
        docManager.assetPrefix = "document-assets";
        docManager.addFont(0, "i", "ff0", "Times New Roman", "normal", "italic");
        docManager.initStyles();
        new Scribd.EmbedsShow("#embed", {document:{page_count:1}});
        docManager.addPage({pageNum:1, fonts:[0]});
    </script>''')
    options, metadata = extract_embed_data(root)
    assert metadata == [{"pageNum": 1, "fonts": [0]}]
    assert options["font_data"] == {
        "fonts": [{"id": 0, "family": "ff0", "weight": "normal", "style": "italic"}],
        "hosts": ["https://html.scribdassets.com"], "asset_prefix": "document-assets",
    }


@pytest.mark.parametrize("document", ["null", "{page_count:true}", "{page_count:10001}"])
def test_invalid_document_metadata_is_rejected(document):
    root = html.fromstring(f'''<script>
        new Scribd.EmbedsShow("#embed", {{document:{document}}});
        docManager.addPage({{pageNum:1}});
    </script>''')
    with pytest.raises(ValueError, match="page count"):
        extract_embed_data(root)


def test_original_font_asset_is_embedded_and_failure_propagates(monkeypatch):
    root = html.fromstring('<style>.text_layer .w { display:inline-block }</style>')
    font_data = {"hosts": ["https://html.scribdassets.com"], "asset_prefix": "assets",
                 "fonts": [{"id": 0, "family": "ff0", "weight": "bold", "style": "italic"}]}
    calls = []

    async def get(session, url, *, base_url):
        calls.append((url, base_url))
        return b"original font bytes", url

    monkeypatch.setattr(pages, "_get", get)
    css = asyncio.run(download_text_style(object(), root, font_data, "https://example.com/", 2))
    assert calls == [("https://html.scribdassets.com/assets/fonts/0000.woff2",
                      "https://example.com/")]
    assert base64.b64encode(b"original font bytes").decode() in css
    assert "font-weight:bold; font-style:italic" in css
    assert ".text_layer .w { display:inline-block }" in css

    async def fail(*args, **kwargs):
        raise RuntimeError("Font unavailable")

    monkeypatch.setattr(pages, "_get", fail)
    with pytest.raises(RuntimeError, match="Font unavailable"):
        asyncio.run(download_text_style(object(), root, font_data, "https://example.com/", 2))


@pytest.mark.parametrize("css", ["", "@font-face {font-family:ff0;src:url(data:font/woff2;base64,YmFk)} .ff0 span{font-family:ff0}"])
def test_bad_font_does_not_silently_render_with_a_substitute(css):
    page = pages.parse_page(html.fromstring('''<div id="page1" style="width:200px;height:200px">
        <div class="text_layer" style="font-size:50px">
          <div class="ff0"><span class="a" style="left:10px;top:10px">Test</span></div>
        </div></div>'''), 1, "https://example.com/")
    with pytest.raises(Exception, match="NetworkError|font"):
        render_text_pdf([page], 200, css)


def test_native_font_spacers_kerning_unicode_and_page_boundaries(tmp_path):
    pdfium = pytest.importorskip("pypdfium2")
    font = (Path(reportlab.__file__).parent / "fonts" / "Vera.ttf").read_bytes()
    css = ("@font-face {font-family:ff0;src:url(data:font/ttf;base64,"
           + base64.b64encode(font).decode() + ") format('truetype')}"
           ".ff0 span{font-family:ff0;font-kerning:none} .text_layer .w{display:inline-block}"
           ".text_layer .l{display:inline}")
    document = []
    for number, (width, height) in enumerate([(902, 1167), (600, 900), (902, 1167)], 1):
        # Unscaled coordinates extend far beyond a printed page. Text at both
        # ends must survive; rendering a tall DOM loses lines during pagination.
        root = html.fromstring(f'''<div id="page{number}" style="width:{width}px;height:{height}px">
          <div class="text_layer"><div class="ff0" style="font-size:100px">
            <span class="a" style="left:100px;top:100px">A<span class="w" style="width:100px"></span>B<span class="l" style="margin-left:-20px">C</span></span>
            <span class="a" style="left:100px;top:300px">A<span class="w" style="width:0"></span>B<span class="l" style="margin-left:0">C</span></span>
            <span class="a" style="left:100px;top:500px">café Ω —</span>
            <span class="a" style="left:100px;top:{height * 5 - 200}px">End {number}</span>
          </div></div></div>''')
        document.append(pages.parse_page(root, number, "https://example.com/"))
    output = pages._assemble_pdf(document, {}, tmp_path, "Native text", text_style=css)
    reader = PdfReader(output)
    assert len(reader.pages) == 3
    rendered = pdfium.PdfDocument(output)
    for index, pdf_page in enumerate(reader.pages):
        text = pdf_page.extract_text()
        assert "café Ω —" in text
        assert f"End {index + 1}" in text
        assert "?" not in text
        expected_height = document[index].height * 750 / document[index].width
        assert float(pdf_page.mediabox.height) == pytest.approx(expected_height, abs=1)
        # Compare the same glyphs on two lines, independent of font metrics.
        text_page = rendered[index].get_textpage()
        contents = text_page.get_text_range()
        a = [i for i, char in enumerate(contents) if char == "A"][:2]
        b = [i for i, char in enumerate(contents) if char == "B"][:2]
        c = [i for i, char in enumerate(contents) if char == "C"][:2]
        left = lambda pos: text_page.get_charbox(pos)[0]
        scale = 750 / document[index].width * .2
        assert (left(b[0]) - left(a[0])) - (left(b[1]) - left(a[1])) == pytest.approx(100 * scale, abs=.1)
        assert (left(c[0]) - left(a[0])) - (left(c[1]) - left(a[1])) == pytest.approx(80 * scale, abs=.1)
        text_page.close()
    rendered.close()
