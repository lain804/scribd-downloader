import asyncio
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import subprocess
import sys
from threading import Lock, Thread
from time import sleep
from types import SimpleNamespace

from PIL import Image
from curl_cffi.requests import AsyncSession
from lxml import html
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
    state = {"challenge": False, "embed_status": 200, "fail_asset": False, "requests": [],
             "delay": 0, "active": 0, "peak": 0}
    lock = Lock()

    def page(number):
        return f'''<div id="page{number}" style="width:40px;height:60px">
          <img class="absimg" orig="/image.png" style="width:40px;height:60px">
          <div class="text_layer" style="font-size:20px;color:black">
            <span style="left:10px;top:10px">Page {number} visible text</span>
            <span style="left:10px;top:20px;color:transparent">Hidden text</span>
            <span style="left:10px;top:30px;opacity:0.50;filter:alpha(opacity=50)">OCR text</span>
          </div>
        </div>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                self.respond()
            finally:
                with lock:
                    state["active"] -= 1

        def respond(self):
            state["requests"].append(self.path)
            sleep(state["delay"])
            status = 200
            if self.path == "/embed":
                body = pages.CLIENT_CHALLENGE if state["challenge"] else state["html"]
                status = state["embed_status"]
            elif self.path == "/page2" and state.get("redirect_chunk"):
                self.send_response(302)
                self.send_header("Location", "/chunks/page2")
                self.end_headers()
                return
            elif self.path in {"/page2", "/chunks/page2"}:
                markup = page(2)
                if self.path == "/chunks/page2":
                    markup = markup.replace('orig="/image.png"', 'orig="image.png"')
                body = ("page2_callback(" + json.dumps([markup]) + ");").encode()
            elif self.path == "/compressed":
                body = gzip.compress(b"x" * 16384)
            elif self.path == "/chunks/image.png":
                body = b"This is a redirect response"
                self.send_response(302)
                self.send_header("Location", "/alternate/image.png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            elif self.path in {"/image.png", "/alternate/image.png"}:
                body = image_bytes.getvalue()
                if state["fail_asset"]:
                    status, body = 503, b"Image unavailable"
            else:
                status, body = 404, b"Not found"
            self.send_response(status)
            if self.path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
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
    monkeypatch.chdir(tmp_path)
    count = asyncio.run(pages.download_pdf(state["url"]))
    output = tmp_path / "Test Manual.pdf"
    reader = PdfReader(output)
    assert count == len(reader.pages) == 2
    assert reader.metadata.title == "Test Manual"
    for number, pdf_page in enumerate(reader.pages, 1):
        assert f"Page {number} visible text" in pdf_page.extract_text()
        assert "Hidden text" not in pdf_page.extract_text()
        assert "OCR text" not in pdf_page.extract_text()
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


def test_utf8_inline_page_text_survives_download(tmp_path, document_server):
    document_server["html"] = document_server["html"].replace(
        b"Page 1 visible text", "café —".encode("utf-8"),
    )
    output = tmp_path / "unicode.pdf"
    asyncio.run(pages.download_pdf(document_server["url"], output))
    assert "café —" in PdfReader(output).pages[0].extract_text()


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


def test_relative_asset_uses_redirected_chunk_location(tmp_path, document_server):
    document_server["redirect_chunk"] = True
    output = tmp_path / "document.pdf"
    assert asyncio.run(pages.download_pdf(document_server["url"], output)) == 2
    assert "/chunks/image.png" in document_server["requests"]
    assert "/alternate/image.png" in document_server["requests"]


def test_download_limit_applies_to_decompressed_body(document_server):
    async def run():
        base = document_server["url"]
        url = pages._image_url(base, "/compressed")
        async with AsyncSession() as session:
            with pytest.raises(ValueError, match="exceeds 1,024 bytes"):
                await pages._get(session, url, base_url=base, limit=1024)
            contents, _ = await pages._get(session, url, base_url=base, limit=16384)
        assert len(contents) == 16384

    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://assets.example.org/image", "http://scribdassets.com/image"])
def test_asset_request_validates_origin_before_using_session(url):
    async def run():
        with pytest.raises(ValueError, match="Untrusted Scribd"):
            await pages._get(object(), url, base_url="https://www.scribd.com/embeds/1/content")

    asyncio.run(run())


def test_asset_redirect_is_validated_before_following():
    base = "https://www.scribd.com/embeds/1/content"
    asset = "https://html.scribdassets.com/image.png"

    class Session:
        calls = []

        async def get(self, url, *, allow_redirects, **kwargs):
            assert not allow_redirects
            self.calls.append(url)
            return SimpleNamespace(status_code=302, url=url,
                                   headers={"Location": "https://assets.example.org/image.png"})

    session = Session()
    with pytest.raises(ValueError, match="Untrusted Scribd"):
        asyncio.run(pages._get(session, asset, base_url=base))
    assert session.calls == [asset]


def test_assets_start_before_slow_chunks_finish_and_pages_keep_order(monkeypatch, tmp_path):
    def page(number):
        return f'''<div id="page{number}" style="width:40px;height:60px">
            <img class="absimg" src="image{number}.png" style="width:40px;height:60px"></div>'''

    root = html.fromstring(page(1))
    metadata = [{"pageNum": 1}, {"pageNum": 2, "contentUrl": "/chunks/page2"},
                {"pageNum": 3, "contentUrl": "/chunks/page3"}]
    contents = BytesIO()
    with Image.new("RGB", (40, 60), "red") as image:
        image.save(contents, format="PNG")
    requests = []
    completed_chunks = []

    async def run():
        first_image = asyncio.Event()
        third_chunk = asyncio.Event()

        async def get(session, url, *, base_url, headers=None, limit=None, sink=None):
            requests.append(url)
            if url.endswith("/page2"):
                # Would time out with the old fetch-all-chunks-first pipeline.
                await asyncio.wait_for(first_image.wait(), 2)
                await asyncio.wait_for(third_chunk.wait(), 2)
                completed_chunks.append(2)
                return ("page2_callback(" + json.dumps([page(2)]) + ");").encode(), url
            if url.endswith("/page3"):
                third_chunk.set()
                completed_chunks.append(3)
                return ("page3_callback(" + json.dumps([page(3)]) + ");").encode(), url
            assert "image/webp" not in headers["Accept"]
            first_image.set()
            sink.write(contents.getvalue())
            return b"", url

        monkeypatch.setattr(pages, "_get", get)
        return await pages._download_assets(
            object(), root, metadata, 3, "https://example.com/embed", tmp_path, 2,
        )

    document, assets = asyncio.run(run())
    assert [page.number for page in document] == [1, 2, 3]
    assert completed_chunks == [3, 2]
    assert set(assets) == {"https://example.com/image1.png", "https://example.com/chunks/image2.png",
                           "https://example.com/chunks/image3.png"}
    assert all(path.read_bytes() == contents.getvalue() for path in assets.values())


def test_failed_worker_cancels_siblings_before_returning():
    async def run():
        running = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow():
            running.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def fail():
            await running.wait()
            raise ValueError("original error")

        with pytest.raises(ValueError, match="original error"):
            await pages._gather_cancel(slow(), fail())
        assert cancelled.is_set()

    asyncio.run(run())


def test_failed_assembly_preserves_output_and_removes_assets(monkeypatch, tmp_path, document_server):
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"existing document")

    def fail(*args, **kwargs):
        raise RuntimeError("assembly failed")

    monkeypatch.setattr(pages, "_assemble_pdf", fail)
    with pytest.raises(RuntimeError, match="assembly failed"):
        asyncio.run(pages.download_pdf(document_server["url"], output))
    assert output.read_bytes() == b"existing document"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL inheritance")
def test_download_inherits_destination_permissions(tmp_path, document_server):
    # Make the destination distinguishable from Python 3.13's private temp
    # directories, which replace inherited permissions with an owner-only ACL.
    identity = subprocess.check_output(["whoami"], text=True).strip()
    subprocess.run(["icacls", str(tmp_path), "/grant", f"{identity}:(OI)(CI)(RX)"],
                   check=True, capture_output=True)
    baseline = tmp_path / "normal-file"
    baseline.write_bytes(b"normal destination permissions")
    output = tmp_path / "document.pdf"
    asyncio.run(pages.download_pdf(document_server["url"], output))
    script = '''@{
        actual = (Get-Acl -LiteralPath $env:SCRIBD_TEST_OUTPUT).Sddl
        expected = (Get-Acl -LiteralPath $env:SCRIBD_TEST_BASELINE).Sddl
    } | ConvertTo-Json -Compress'''
    acls = json.loads(subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", script], text=True,
        env={**os.environ, "SCRIBD_TEST_OUTPUT": str(output), "SCRIBD_TEST_BASELINE": str(baseline)},
    ))
    assert acls["actual"] == acls["expected"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["document.pdf", "normal-file"]


@pytest.mark.parametrize("concurrency", [0, -1, 257, True, 1.5])
def test_invalid_concurrency_rejected_before_network(concurrency):
    with pytest.raises(ValueError, match="Concurrency"):
        asyncio.run(pages.download_pdf("https://example.com/", concurrency=concurrency))


@pytest.mark.parametrize("concurrency", [1, 2])
def test_connection_limit_is_shared_by_chunks_and_images(tmp_path, document_server, concurrency):
    document_server["delay"] = 0.05
    asyncio.run(pages.download_pdf(
        document_server["url"], tmp_path / "document.pdf", concurrency=concurrency,
    ))
    assert document_server["peak"] == concurrency


def test_long_unicode_title_fits_filesystem_filename_limit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    output = pages._default_output_path("文" * 200)
    assert len(output.name.encode("utf-8")) <= 255
    output.touch()


def test_output_directory_preserves_existing_pdfs_and_handles_repeated_titles(tmp_path, document_server):
    directory = tmp_path / "downloads"
    directory.mkdir()
    original = directory / "Test Manual.pdf"
    original.write_bytes(b"existing PDF")

    async def run():
        for _ in range(2):
            await pages.download_pdf(document_server["url"], output_dir=directory, overwrite=False)

    asyncio.run(run())
    assert original.read_bytes() == b"existing PDF"
    for name in ("Test Manual (1).pdf", "Test Manual (2).pdf"):
        assert len(PdfReader(directory / name).pages) == 2
    assert sorted(path.name for path in directory.iterdir()) == [
        "Test Manual (1).pdf", "Test Manual (2).pdf", "Test Manual.pdf",
    ]


def test_output_file_and_directory_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="either an output PDF path or an output directory"):
        asyncio.run(pages.download_pdf("https://example.com/", tmp_path / "file.pdf", output_dir=tmp_path))


def test_download_progress_reports_ready_pages_assembly_and_success(tmp_path, document_server):
    output = tmp_path / "document.pdf"
    events = []

    def progress(phase, completed, total):
        events.append((phase, completed, total))
        if phase == "Saved PDF":
            assert output.is_file(), "Success must only be reported after replacing the output"

    asyncio.run(pages.download_pdf(document_server["url"], output, on_progress=progress))
    downloads = [done for phase, done, total in events if phase == "Downloading pages"]
    assert downloads == sorted(set(downloads))
    assert downloads[0] == 0 and downloads[-1] == 2
    assert [done for phase, done, total in events if phase == "Assembling PDF"] == [0, 1, 2]
    assert all(total == 2 for _, _, total in events)
    assert events[-1] == ("Saved PDF", 2, 2)


def test_progress_waits_for_all_assets_and_handles_shared_cached_and_text_pages(monkeypatch, tmp_path):
    root = html.fromstring('''<main>
        <div id="page1" style="width:40px;height:60px">
          <img class="absimg" src="/a.png" style="width:40px;height:60px">
          <img class="absimg" src="/a.png" style="width:40px;height:60px">
          <img class="absimg" src="/b.png" style="width:40px;height:60px">
        </div>
        <div id="page3" style="width:40px;height:60px">
          <div class="text_layer" style="font-size:20px">
            <span style="left:0;top:0">Text only</span>
          </div>
        </div></main>''')
    metadata = [{"pageNum": 1}, {"pageNum": 2, "contentUrl": "/page2"}, {"pageNum": 3}]
    events = []
    buffer = BytesIO()
    with Image.new("RGB", (40, 60), "red") as image:
        image.save(buffer, format="PNG")

    async def run():
        image_a = asyncio.Event()
        cached_page_ready = asyncio.Event()

        def progress(phase, done, total):
            events.append((phase, done, total))
            if done == 2:
                cached_page_ready.set()

        async def get(session, url, *, base_url, headers=None, limit=None, sink=None):
            if url.endswith("/page2"):
                await asyncio.wait_for(image_a.wait(), 2)
                page = '''<div id="page2" style="width:40px;height:60px">
                    <img class="absimg" src="/a.png" style="width:40px;height:60px"></div>'''
                return ("page2_callback(" + json.dumps([page]) + ");").encode(), url
            if url.endswith("/a.png"):
                image_a.set()
            else:
                await asyncio.wait_for(cached_page_ready.wait(), 2)
            sink.write(buffer.getvalue())
            return b"", url

        monkeypatch.setattr(pages, "_get", get)
        await pages._download_assets(object(), root, metadata, 3, "https://example.com/embed",
                                     tmp_path, 2, on_progress=progress)

    asyncio.run(run())
    assert events == [("Downloading pages", number, 3) for number in range(4)]
