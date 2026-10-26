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
        waited=False,
    )

    async def ensure():
        pass

    monkeypatch.setattr(pages, "_ensure_chromium", ensure)

    class Page:
        main_frame = object()

        def on(self, event, handler):
            self.handler = handler

        async def wait_for_function(self, expression, **kwargs):
            state.waited = True
            if pages.CLIENT_CHALLENGE in state.contents:
                raise TimeoutError("Challenge never cleared")

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
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed = True

        async def fetch(self, url, *, page_setup, page_action, **kwargs):
            page = Page()
            await page_setup(page)
            if state.navigation:
                page.handler(Response(page.main_frame, True, pages.CLIENT_CHALLENGE))
                page.handler(Response(page.main_frame, True))
            # Ignore both iframe navigations and non-navigation responses.
            page.handler(Response(object(), True))
            page.handler(Response(page.main_frame, False))
            try:
                await page_action(page)
            except TimeoutError:
                # Scrapling logs callback failures and returns a response anyway.
                pass
            return SimpleNamespace(body=b"<html>mutated browser DOM</html>")

    monkeypatch.setattr(fetchers, "AsyncStealthySession", Browser)
    return state


def test_challenge_returns_original_navigation_html(browser):
    result = asyncio.run(pages._pass_client_challenge("https://www.scribd.com/embeds/1/content", 5))
    assert result == browser.contents
    assert browser.waited
    assert browser.closed


@pytest.mark.parametrize(
    ("contents", "status", "navigation", "message"),
    [
        (pages.CLIENT_CHALLENGE, 200, True, "did not complete successfully"),
        (b"Not found", 404, True, "HTTP 404"),
        (b"", 200, False, "no document response"),
    ],
)
def test_browser_failures_are_reported(browser, contents, status, navigation, message):
    browser.contents, browser.status, browser.navigation = contents, status, navigation
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(pages._pass_client_challenge("https://www.scribd.com/embeds/1/content", 5))
    assert browser.closed
