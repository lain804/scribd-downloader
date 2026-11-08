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
    monkeypatch.setattr(async_api, "async_playwright", lambda: BrowserDriver(executable))

    async def unexpected_install(*args, **kwargs):
        pytest.fail("An existing browser must not trigger installation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_install)
    asyncio.run(pages._ensure_chromium())


@pytest.mark.parametrize("returncode", [0, 1])
def test_missing_chromium_installs_with_tool_python(monkeypatch, tmp_path, returncode):
    monkeypatch.setattr(
        async_api, "async_playwright", lambda: BrowserDriver(tmp_path / "missing.exe")
    )
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
            asyncio.run(pages._ensure_chromium())
    else:
        asyncio.run(pages._ensure_chromium())
    assert commands == [(sys.executable, "-m", "patchright", "install", "chromium")]


@pytest.fixture
def browser(monkeypatch):
    state = SimpleNamespace(
        contents=b"<html><title>Scribd</title><div>original document</div></html>",
        status=200,
        navigation=True,
        closed=False,
        navigation_options=None,
        navigation_error=False,
    )

    async def ensure():
        pass

    monkeypatch.setattr(pages, "_ensure_chromium", ensure)

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

    class Response:
        def __init__(self, frame, navigation, contents=None):
            self.frame = frame
            self.request = SimpleNamespace(is_navigation_request=lambda: navigation)
            self.status = state.status
            self.contents = contents

        async def body(self):
            assert not state.closed, "Read response bytes before closing the browser"
            return state.contents if self.contents is None else self.contents

    class Browser:
        def __init__(self, **kwargs):
            self.context = self

        async def new_page(self):
            return Page()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed = True

    monkeypatch.setattr(fetchers, "AsyncStealthySession", Browser)
    return state


def test_challenge_returns_original_navigation_html(browser):
    result = asyncio.run(pages._pass_client_challenge("https://www.scribd.com/embeds/1/content", 5))
    assert result == browser.contents
    assert browser.navigation_options == {"wait_until": "commit", "timeout": 5000}
    assert browser.closed


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
        asyncio.run(pages._pass_client_challenge("https://www.scribd.com/embeds/1/content", 0.01))
    assert browser.closed


def test_navigation_timeout_closes_browser(browser):
    browser.navigation_error = True
    with pytest.raises(RuntimeError, match="did not complete successfully"):
        asyncio.run(pages._pass_client_challenge("https://www.scribd.com/embeds/1/content", 0.01))
    assert browser.closed
