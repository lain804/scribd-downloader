from io import BytesIO
import shutil
import subprocess

from lxml import html
from PIL import Image
import pikepdf
from pypdf import PdfReader
import pytest

from scribd_downloader import pages


def asset(tmp_path, image, format="PNG"):
    path = tmp_path / "source.image"
    image.save(path, format=format)
    return path


def assemble(tmp_path, placements, *, text=(), width=100, height=100, count=1):
    document = [pages.Page(n, width, height, tuple(placements), text) for n in range(1, count + 1)]
    return pages._assemble_pdf(document, {"asset": tmp_path / "source.image"}, tmp_path, "Test")


def test_jpeg_bytes_and_shared_image_are_preserved(tmp_path):
    with Image.new("RGB", (1200, 1600), "red") as image:
        original = asset(tmp_path, image, "JPEG").read_bytes()
    placement = pages.Placement("asset", 0, 0, 100, 100, (0, 100, 100, 0))
    output = assemble(tmp_path, [placement, placement], count=20)
    with pikepdf.Pdf.open(output) as pdf:
        images = [page.Resources.XObject.Im0 for page in pdf.pages]
        assert len(pdf.pages) == 20
        assert len({image.objgen for image in images}) == 1
        assert all(len(page.Resources.XObject) == 1 for page in pdf.pages)
        assert images[0].Filter == pikepdf.Name.DCTDecode
        assert images[0].read_raw_bytes() == original
        assert (images[0].Width, images[0].Height) == (1200, 1600)
    assert output.stat().st_size < len(original) + 20_000


@pytest.mark.parametrize("mode", ["1", "L", "P", "RGB", "RGBA"])
def test_png_modes_remain_lossless(tmp_path, mode):
    with Image.new(mode, (120, 160)) as image:
        image.putpixel((10, 20), {"1": 1, "L": 130, "P": 2,
                               "RGB": (23, 45, 67), "RGBA": (23, 45, 67, 89)}[mode])
        asset(tmp_path, image)
    placement = pages.Placement("asset", 0, 0, 100, 100, (0, 100, 100, 0))
    output = assemble(tmp_path, [placement])
    with Image.open(tmp_path / "source.image") as original:
        actual = PdfReader(output).pages[0].images[0].image
        assert actual.size == original.size
        assert actual.convert("RGBA").tobytes() == original.convert("RGBA").tobytes()


def test_text_is_searchable_but_invisible(tmp_path):
    with Image.new("RGB", (100, 100), "white") as image:
        asset(tmp_path, image)
    placement = pages.Placement("asset", 0, 0, 100, 100, (0, 100, 100, 0))
    text = ((0, 0, 50, None, "Search this text"),)
    output = assemble(tmp_path, [placement], text=text)
    page = PdfReader(output).pages[0]
    assert "Search this text" in page.extract_text()
    assert b"3 Tr" in page.get_contents().get_data()
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-scale-to", "100", "-singlefile", "-png", str(output),
                        str(tmp_path / "text")], check=True, capture_output=True)
        with Image.open(tmp_path / "text.png") as rendered:
            assert rendered.convert("RGB").getextrema() == ((255, 255),) * 3


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="Poppler needed to verify PDF clipping")
def test_clipping_position_scale_transparency_and_page_edges(tmp_path):
    # The source is higher resolution than its CSS size. Draw only its green
    # quadrant, partly off-page, then a translucent red image over it.
    with Image.new("RGBA", (200, 200), (255, 0, 0, 128)) as image:
        for x in range(100, 200):
            for y in range(100, 200):
                image.putpixel((x, y), (0, 255, 0, 255))
        asset(tmp_path, image)
    placements = [
        pages.Placement("asset", -60, -60, 100, 100, (50, 100, 100, 50)),
        pages.Placement("asset", 10, 10, 100, 100, (0, 40, 40, 0)),
        pages.Placement("asset", 0, 0, 100, 100, (90, 10, 10, 90)),  # empty clip
    ]
    output = assemble(tmp_path, placements)
    subprocess.run(["pdftoppm", "-scale-to", "100", "-singlefile", "-png", str(output),
                    str(tmp_path / "clips")], check=True, capture_output=True)
    with Image.open(tmp_path / "clips.png") as rendered:
        assert rendered.getpixel((5, 5)) == (0, 255, 0)
        assert rendered.getpixel((20, 20)) in {(128, 127, 0), (128, 126, 0)}
        assert rendered.getpixel((45, 45)) in {(255, 127, 127), (255, 126, 126)}
        assert rendered.getpixel((60, 60)) == (255, 255, 255)


def test_oversized_image_is_rejected_before_decoding(monkeypatch):
    contents = BytesIO()
    with Image.new("RGB", (1, 1)) as image:
        image.save(contents, format="PNG")
    original = Image.open

    def oversized(*args, **kwargs):
        image = original(*args, **kwargs)
        image._size = (10_000, 10_000)

        def unexpected_decode(*args, **kwargs):
            pytest.fail("An oversized image must not be decoded")

        image.load = unexpected_decode
        return image

    monkeypatch.setattr(Image, "open", oversized)
    with pytest.raises(ValueError, match="40 million pixels"):
        pages._validate_asset(contents.getvalue(), "https://example.com/large.png")


def test_span_own_font_size_is_used():
    root = html.fromstring('''<div id="page1" style="width:100px;height:100px">
        <img class="absimg" src="image.png" style="width:100px;height:100px">
        <div class="text_layer"><span style="left:2px;top:3px;font-size:20px">Text</span></div>
        </div>''')
    assert pages.parse_page(root, 1, "https://example.com/").text_runs[0][2] == 20


@pytest.mark.parametrize("css", ["color:transparent", "opacity:0"])
def test_transparent_text_visibility_is_inherited(css):
    root = html.fromstring(f'''<div id="page1" style="width:100px;height:100px">
        <div class="text_layer" style="font-size:20px;{css}">
            <span style="left:2px;top:3px">Hidden but searchable</span></div></div>''')
    assert pages.parse_page(root, 1, "https://example.com/").text_runs[0][3] is None


def test_text_only_pages_keep_visible_content_and_title(tmp_path):
    root = html.fromstring('''<div id="page1" style="width:100px;height:100px">
        <div class="text_layer" style="font-size:100px;color:#ff0000">
            <span style="left:25px;top:25px">Visible text</span></div></div>''')
    page = pages.parse_page(root, 1, "https://example.com/")
    assert not page.placements
    assert page.text_runs[0][3] == (1, 0, 0)
    output = pages._assemble_pdf([page], {}, tmp_path, "Text only")
    reader = PdfReader(output)
    assert reader.metadata.title == "Text only"
    assert "Visible text" in reader.pages[0].extract_text()
    assert b"3 Tr" not in reader.pages[0].get_contents().get_data()
    assert not reader.pages[0].images
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-scale-to", "100", "-singlefile", "-png", str(output),
                        str(tmp_path / "visible")], check=True, capture_output=True)
        with Image.open(tmp_path / "visible.png") as rendered:
            assert rendered.convert("RGB").getextrema() != ((255, 255),) * 3
