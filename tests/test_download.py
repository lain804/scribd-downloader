import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import subprocess
import sys
from threading import Lock, Thread
from time import sleep

from PIL import Image
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

        async def get(session, url, *, headers=None):
            requests.append(url)
            if url.endswith("/page2"):
                # Would time out with the old fetch-all-chunks-first pipeline.
                await asyncio.wait_for(first_image.wait(), 2)
                await asyncio.wait_for(third_chunk.wait(), 2)
                completed_chunks.append(2)
                return ("page2_callback(" + json.dumps([page(2)]) + ");").encode()
            if url.endswith("/page3"):
                third_chunk.set()
                completed_chunks.append(3)
                return ("page3_callback(" + json.dumps([page(3)]) + ");").encode()
            assert "image/webp" not in headers["Accept"]
            first_image.set()
            return contents.getvalue()

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

        async def get(session, url, *, headers=None):
            if url.endswith("/page2"):
                await asyncio.wait_for(image_a.wait(), 2)
                page = '''<div id="page2" style="width:40px;height:60px">
                    <img class="absimg" src="/a.png" style="width:40px;height:60px"></div>'''
                return ("page2_callback(" + json.dumps([page]) + ");").encode()
            if url.endswith("/a.png"):
                image_a.set()
            else:
                await asyncio.wait_for(cached_page_ready.wait(), 2)
            return buffer.getvalue()

        monkeypatch.setattr(pages, "_get", get)
        await pages._download_assets(object(), root, metadata, 3, "https://example.com/embed",
                                     tmp_path, 2, on_progress=progress)

    asyncio.run(run())
    assert events == [("Downloading pages", number, 3) for number in range(4)]
