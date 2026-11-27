import asyncio
import sys
from types import SimpleNamespace

import pytest
from patchright import async_api
from scrapling import fetchers

from scribd_downloader import pages


class BrowserDriver:
    def __init__(self, executable):
        self.chromium = SimpleNamespace(executable_path=str(executable))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_existing_chromium_does_not_install(monkeypatch, tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.touch()

    async def unexpected_install(*args, **kwargs):
        pytest.fail("An existing browser must not trigger installation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_install)
    asyncio.run(pages._ensure_chromium(executable))


@pytest.mark.parametrize("returncode", [0, 1])
def test_missing_chromium_installs_with_tool_python(monkeypatch, tmp_path, returncode):
    executable = tmp_path / "missing.exe"
    commands = []

    class Installer:
        async def communicate(self):
            return b"Browser download failed", None

    installer = Installer()
    installer.returncode = returncode

    async def install(*args, **kwargs):
        commands.append(args)
        return installer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", install)
    if returncode:
        with pytest.raises(RuntimeError, match="Could not install Chromium.*Browser download failed"):
            asyncio.run(pages._ensure_chromium(executable))
    else:
        asyncio.run(pages._ensure_chromium(executable))
    assert commands == [(sys.executable, "-m", "patchright", "install", "chromium")]


@pytest.fixture
def browser(monkeypatch):
    state = SimpleNamespace(
        contents=b"<html><title>Scribd</title><div>original document</div></html>",
        status=200,
        navigation=True,
        closed=False,
        sandbox=None,
        navigation_options=None,
        navigation_error=False,
        discard_challenge_body=False,
        launches=0,
        checked=[],
        opened_pages=[],
        closed_pages=[],
    )
    executable = "/test/chromium"

    async def check_chromium(path):
        state.checked.append(str(path))
        await asyncio.sleep(0)

    monkeypatch.setattr(pages, "_ensure_chromium", check_chromium)
    monkeypatch.setattr(async_api, "async_playwright", lambda: BrowserDriver(executable))

    class Page:
        main_frame = object()

        def on(self, event, handler):
            self.handler = handler

        async def wait_for_function(self, expression, **kwargs):
            pytest.fail("Scribd's Content Security Policy blocks eval-based waits")

        async def goto(self, url, **kwargs):
            state.navigation_options = kwargs
            if state.navigation_error:
                raise async_api.TimeoutError("Navigation timed out")
            # Full page load may never finish despite receiving valid HTML.
            assert kwargs["wait_until"] == "commit"
            if state.navigation:
                self.handler(Response(self.main_frame, True, pages.CLIENT_CHALLENGE))
                self.handler(Response(self.main_frame, True))
            # Ignore both iframe navigations and non-navigation responses.
            self.handler(Response(object(), True))
            self.handler(Response(self.main_frame, False))

        async def close(self):
            state.closed_pages.append(self)

    class Response:
        def __init__(self, frame, navigation, contents=None):
            self.frame = frame
            self.request = SimpleNamespace(is_navigation_request=lambda: navigation)
            self.status = state.status
            self.contents = contents

        async def body(self):
            assert not state.closed, "Read response bytes before closing the browser"
            if state.discard_challenge_body and self.contents == pages.CLIENT_CHALLENGE:
                raise async_api.Error("Network.getResponseBody: No resource with given identifier found")
            return state.contents if self.contents is None else self.contents

    class Browser:
        def __init__(self, **kwargs):
            self.context = self
            state.sandbox = kwargs["additional_args"]["chromium_sandbox"]

        def is_closed(self):
            return state.closed

        async def new_page(self):
            page = Page()
            state.opened_pages.append(page)
            return page

        async def __aenter__(self):
            state.launches += 1
            return self

        async def __aexit__(self, *args):
            state.closed = True

    monkeypatch.setattr(fetchers, "AsyncStealthySession", Browser)
    return state


async def run_challenge(timeout):
    async with pages._DownloadSession(timeout=timeout) as session:
        return await pages._pass_client_challenge(session, "https://www.scribd.com/embeds/1/content")


def test_challenge_returns_original_navigation_html(browser):
    result = asyncio.run(run_challenge(5))
    assert result == browser.contents
    assert browser.navigation_options == {"wait_until": "commit", "timeout": 5000}
    assert browser.closed and browser.sandbox


@pytest.mark.parametrize(
    ("contents", "status", "navigation", "message"),
    [
        (pages.CLIENT_CHALLENGE, 200, True, "did not complete successfully"),
        (b"Not found", 404, True, "HTTP 404"),
        (b"", 200, False, "did not complete successfully"),
    ],
)
def test_browser_failures_are_reported(browser, contents, status, navigation, message):
    browser.contents, browser.status, browser.navigation = contents, status, navigation
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(run_challenge(0.01))
    assert browser.closed_pages == browser.opened_pages
    assert browser.closed


def test_navigation_timeout_closes_browser(browser):
    browser.navigation_error = True
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        asyncio.run(run_challenge(0.01))
    assert browser.closed_pages == browser.opened_pages
    assert browser.closed


def test_batch_challenges_reuse_one_browser_and_close_each_tab(browser):
    browser.discard_challenge_body = True

    async def run():
        async with pages._DownloadSession() as session:
            contents = await asyncio.gather(*(
                pages._pass_client_challenge(session, f"https://www.scribd.com/embeds/{number}/content")
                for number in (1, 2)
            ))
            assert contents == [browser.contents, browser.contents]
            url = "https://www.scribd.com/embeds/3/content"
            browser.navigation_error = True
            with pytest.raises(RuntimeError, match="did not complete successfully"):
                await pages._pass_client_challenge(session, url)
            browser.navigation_error = False
            assert await pages._pass_client_challenge(session, url) == browser.contents
            assert not browser.closed
            assert len(browser.closed_pages) == len(browser.opened_pages) == 4
            assert set(browser.closed_pages) == set(browser.opened_pages)

    asyncio.run(run())
    assert browser.closed and browser.launches == 1
    assert browser.checked == ["/test/chromium"]
