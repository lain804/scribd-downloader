import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread

from PIL import Image
from pypdf import PdfReader
import pytest
from curl_cffi.requests.exceptions import HTTPError

from scribd_downloader import pages


@pytest.fixture
def document_server():
    image = Image.new("RGB", (40, 60), "red")
    image_bytes = BytesIO()
    image.save(image_bytes, format="PNG")
    image.close()
    state = {"challenge": False, "embed_status": 200, "fail_asset": False, "requests": []}

    def page(number):
        return f'''<div id="page{number}" style="width:40px;height:60px">
          <img class="absimg" orig="/image.png" style="width:40px;height:60px">
          <div class="text_layer" style="font-size:20px;color:black">
            <span style="left:10px;top:10px">Page {number} searchable text</span>
          </div>
        </div>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(self.path)
            status = 200
            if self.path == "/embed":
                body = pages.CLIENT_CHALLENGE if state["challenge"] else state["html"]
                status = state["embed_status"]
            elif self.path == "/page2":
                body = ("page2_callback(" + json.dumps([page(2)]) + ");").encode()
            elif self.path == "/image.png":
                body = image_bytes.getvalue()
                if state["fail_asset"]:
                    status, body = 503, b"Image unavailable"
            else:
                status, body = 404, b"Not found"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    state["url"] = base + "/embed"
    state["html"] = (f'''<html><title>Scribd</title><body>{page(1)}
      <script>
        new Scribd.EmbedsShow("#embed", {{document: {{title: "Test Manual", page_count: 2}}}});
        docManager.addPage({{pageNum: 1}});
        docManager.addPage({{pageNum: 2, contentUrl: "{base}/page2"}});
        docManager.allPagesAdded();
      </script></body></html>''').encode()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(("challenge", "status"), [(False, 200), (True, 200), (True, 403)])
def test_download_assembles_all_pages_without_replaying_challenge(
    monkeypatch, tmp_path, document_server, challenge, status
):
    state = document_server
    state["challenge"] = challenge
    state["embed_status"] = status
    calls = []

    async def browser(url, timeout):
        calls.append(url)
        return state["html"]

    monkeypatch.setattr(pages, "_pass_client_challenge", browser)
    monkeypatch.setattr(pages, "VIEWER_WIDTH", 40)
    monkeypatch.setattr(pages, "OUTPUT_SCALE", 1)
    monkeypatch.chdir(tmp_path)
    count = asyncio.run(pages.download_pdf(state["url"]))
    output = tmp_path / "Test Manual.pdf"
    reader = PdfReader(output)
    assert count == len(reader.pages) == 2
    assert reader.metadata.title == "Test Manual"
    for number, pdf_page in enumerate(reader.pages, 1):
        assert f"Page {number} searchable text" in pdf_page.extract_text()
        assert len(pdf_page.images) == 1
        assert pdf_page.images[0].image.getpixel((0, 0)) == (255, 0, 0)
    assert calls == ([state["url"]] if challenge else [])
    assert state["requests"].count("/embed") == 1
    assert state["requests"].count("/image.png") == 1
    assert "/page2" in state["requests"]
    assert list(tmp_path.iterdir()) == [output]


def test_failed_download_preserves_existing_output(tmp_path, document_server):
    document_server["fail_asset"] = True
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"existing document")
    with pytest.raises(HTTPError, match="503"):
        asyncio.run(pages.download_pdf(document_server["url"], output))
    assert output.read_bytes() == b"existing document"
    assert list(tmp_path.iterdir()) == [output]


def test_http_error_without_challenge_does_not_launch_browser(
    monkeypatch, tmp_path, document_server
):
    document_server["embed_status"] = 404

    async def unexpected_browser(*args):
        pytest.fail("A regular HTTP error must not launch Chromium")

    monkeypatch.setattr(pages, "_pass_client_challenge", unexpected_browser)
    with pytest.raises(HTTPError, match="404"):
        asyncio.run(pages.download_pdf(document_server["url"], tmp_path / "missing.pdf"))
    assert not list(tmp_path.iterdir())
