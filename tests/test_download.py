import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import subprocess
import sys
from threading import Barrier, Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace

from PIL import Image
from curl_cffi.requests import Response
from lxml import html
from pypdf import PdfReader
import pytest
from curl_cffi.requests.exceptions import HTTPError

from scribd_downloader import __main__ as cli, pages


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
            headers = {}
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
                if state.get("throttle_asset") and state["requests"].count(self.path) == 1:
                    status, body = 429, b"Try again after this error body."
                    headers["Retry-After"] = "0"
                elif state["fail_asset"]:
                    status, body = 503, b"Image unavailable"
            else:
                status, body = 404, b"Not found"
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
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
        async with pages._DownloadSession() as session:
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

    calls = []

    async def run():
        async def get(url, *, allow_redirects, **kwargs):
            assert not allow_redirects
            calls.append(url)
            return SimpleNamespace(status_code=302, url=url,
                                   headers={"Location": "https://assets.example.org/image.png"})

        async with pages._DownloadSession() as session:
            session.get = get
            await pages._get(session, asset, base_url=base)

    with pytest.raises(ValueError, match="Untrusted Scribd"):
        asyncio.run(run())
    assert calls == [asset]


def test_retried_download_truncates_image_error_body(tmp_path, document_server):
    document_server["throttle_asset"] = True
    base = document_server["url"]
    url = pages._image_url(base, "/image.png")
    asset = tmp_path / "image.png"

    async def run():
        async with pages._DownloadSession() as session:
            with asset.open("wb") as sink:
                await pages._get(session, url, base_url=base, sink=sink)
            assert document_server["requests"].count("/image.png") == 2
            contents, _ = await pages._get(session, url, base_url=base)
            assert asset.read_bytes() == contents

    asyncio.run(run())


@pytest.mark.parametrize("value,expected", [
    (" 3 ", 3), ("Thu, 01 Jan 1970 00:00:12 GMT", 2),
    ("Thu Jan  1 00:00:08 1970", 0), ("invalid", None),
])
def test_retry_after_delays(monkeypatch, value, expected):
    monkeypatch.setattr(pages, "time", lambda: 10)
    assert pages._retry_after(value) == expected


@pytest.mark.parametrize("status,retry_after,attempts", [(429, "0", 4), (429, "3600", 1), (503, "", 1)])
def test_retry_budget_and_long_cooldown(status, retry_after, attempts):
    url = "https://www.scribd.com/embed"
    calls = []

    async def run():
        async def get(url, **kwargs):
            calls.append(url)
            response = Response()
            response.status_code = status
            response.ok = False
            response.headers["Retry-After"] = retry_after
            return response

        async with pages._DownloadSession() as session:
            session.get = get
            with pytest.raises(HTTPError):
                await pages._get(session, url, base_url=url)
            if retry_after == "3600":
                with pytest.raises(HTTPError, match="cooldown"):
                    await pages._get(session, url + "/other", base_url=url)

    asyncio.run(run())
    assert len(calls) == attempts


@pytest.mark.parametrize("status,retry_after,delay", [(429, "", 0.5), (503, "1", 1)])
def test_rate_limit_pauses_queued_requests_only_for_affected_origin(monkeypatch, status, retry_after, delay):
    base = "https://www.scribd.com/embed"
    cdn = "https://html.scribdassets.com/image.png"
    other = "https://www.scribd.com/other"
    calls = []
    monkeypatch.setattr(pages, "uniform", lambda *_: 0)

    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def get(url, **kwargs):
            response = Response()
            response.url = url
            response.status_code = 200
            if not calls:
                assert url == base
                calls.append((url, monotonic()))
                started.set()
                await finish.wait()
                response.status_code = status
                response.headers["Retry-After"] = retry_after
            else:
                calls.append((url, monotonic()))
            return response

        async with pages._DownloadSession(concurrency=1) as session:
            session.get = get
            first = asyncio.create_task(pages._get(session, base, base_url=base))
            await started.wait()
            second = asyncio.create_task(pages._get(session, other, base_url=base))
            third = asyncio.create_task(pages._get(session, cdn, base_url=base))
            released = monotonic()
            finish.set()
            await asyncio.gather(first, second, third)
        return released

    released = asyncio.run(run())
    assert calls[1][0] == cdn
    assert {url for url, _ in calls[2:]} == {base, other}
    assert all(when >= released + delay for _, when in calls[2:])


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


def test_visible_text_fonts_download_while_images_are_pending(monkeypatch, tmp_path, document_server):
    from scribd_downloader import text

    get = pages._get

    async def run():
        image_started = asyncio.Event()
        fonts_loaded = asyncio.Event()

        async def wait_for_fonts(session, url, **kwargs):
            if url.endswith("/image.png"):
                image_started.set()
                await asyncio.wait_for(fonts_loaded.wait(), 2)
            return await get(session, url, **kwargs)

        async def fetch_fonts(*args):
            await asyncio.wait_for(image_started.wait(), 2)
            fonts_loaded.set()
            return "test font CSS"

        async def ensure_chromium():
            pass

        monkeypatch.setattr(pages, "_get", wait_for_fonts)
        monkeypatch.setattr(pages, "_ensure_chromium", ensure_chromium)
        monkeypatch.setattr(text, "download_text_style", fetch_fonts)
        return await pages.download_pdf(document_server["url"], tmp_path / "result.pdf")

    def assemble(document, assets, directory, title, *, destination, text_style, **kwargs):
        assert text_style == "test font CSS"
        assert len(document) == 2 and len(assets) == 1
        destination.write_bytes(b"staged")

    monkeypatch.setattr(pages, "_assemble_pdf", assemble)
    assert asyncio.run(run()) == 2


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


def test_cancelled_assembly_waits_before_cleaning_up(monkeypatch, tmp_path, document_server):
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"previous output")

    async def run():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        finish = Event()
        assembled = Event()

        def assemble(document, assets, directory, title, *, destination, **kwargs):
            loop.call_soon_threadsafe(started.set)
            assert finish.wait(timeout=5)
            assert directory.is_dir()
            destination.write_bytes(b"cancelled output")
            assembled.set()

        monkeypatch.setattr(pages, "_assemble_pdf", assemble)
        download = asyncio.create_task(pages.download_pdf(document_server["url"], output))
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            download.cancel()
            await asyncio.sleep(0)
            download.cancel()
            await asyncio.sleep(0)
            assert not download.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await download
        assert assembled.is_set()

    asyncio.run(run())
    assert output.read_bytes() == b"previous output"
    assert list(tmp_path.iterdir()) == [output]


def test_concurrent_publication_assigns_distinct_files(tmp_path):
    original = tmp_path / "document.pdf"
    original.write_bytes(b"previous output")
    ready = Barrier(2)

    def publish(number):
        staged = tmp_path / f"stage-{number}"
        staged.write_bytes(str(number).encode())
        ready.wait(timeout=5)
        return pages._publish_new_pdf(staged, original)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(publish, (1, 2)))
    assert {path.name for path in outputs} == {"document (1).pdf", "document (2).pdf"}
    assert [path.read_bytes() for path in outputs] == [b"1", b"2"]
    assert original.read_bytes() == b"previous output"


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


def test_batch_shares_connections_and_preserves_colliding_outputs(monkeypatch, tmp_path, document_server):
    document_server["delay"] = 0.05
    document_server["html"] = b'''<div id="page1" style="width:40px;height:60px">
        <img class="absimg" src="/image.png" style="width:40px;height:60px"></div>
        <script>
          new Scribd.EmbedsShow("#embed", {document: {title: "Test Manual", page_count: 1}});
          docManager.addPage({pageNum: 1});
          docManager.allPagesAdded();
        </script>'''

    async def download(session, url, **kwargs):
        return await pages._download_pdf(session, document_server["url"], **kwargs)

    monkeypatch.setattr(cli, "_download_pdf", download)
    documents = [(f"https://www.scribd.com/doc/{number}", str(number)) for number in range(1, 7)]
    assert asyncio.run(cli._download_batch(documents, tmp_path, 2)) == (6, 0)
    assert document_server["peak"] == 2
    outputs = sorted(tmp_path.glob("*.pdf"))
    assert len(outputs) == 6
    assert all(len(PdfReader(pdf).pages) == 1 for pdf in outputs)


def test_long_unicode_title_fits_filesystem_filename_limit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    output = pages._default_output_path("文" * 200)
    assert len(output.name.encode("utf-8")) <= 255
    output.touch()


def test_reserved_filename_gets_a_usable_prefix(tmp_path):
    assert pages._default_output_path("NUL", tmp_path) == tmp_path / "_NUL.pdf"


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
