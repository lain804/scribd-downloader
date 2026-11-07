from io import StringIO
import logging
import sys

import pytest

from scribd_downloader import __main__ as cli
from scribd_downloader import progress


class Terminal(StringIO):
    def isatty(self):
        return True


def test_bar_throttles_redraws_but_keeps_phase_changes_and_final_counts(monkeypatch):
    stream = Terminal()
    now = [0.0]
    monkeypatch.setattr(progress, "perf_counter", lambda: now[0])
    with progress.ProgressBar(stream=stream) as update:
        update("Downloading pages", 0, 10)
        now[0] = 0.01
        update("Downloading pages", 1, 10)
        now[0] = 0.2
        update("Downloading pages", 9, 10)
        now[0] = 0.21
        update("Downloading pages", 10, 10)
        update("Assembling PDF", 0, 10)
        update("Saved PDF", 10, 10)
    output = stream.getvalue()
    assert "0/10 pages" in output
    assert "1/10 pages" not in output
    assert "9/10 pages" in output
    assert "Saved PDF [####################] 10/10 pages" in output
    assert output.endswith("\n")


@pytest.mark.parametrize("stream,enabled", [(StringIO(), True), (Terminal(), False)])
def test_non_terminal_and_disabled_progress_have_no_output(stream, enabled):
    with progress.ProgressBar(stream=stream, enabled=enabled) as update:
        assert update is None
    assert stream.getvalue() == ""


def test_failed_download_finishes_line_with_latest_page_count():
    stream = Terminal()
    with pytest.raises(RuntimeError, match="Download failed"):
        with progress.ProgressBar(stream=stream) as update:
            update("Downloading pages", 3, 10)
            raise RuntimeError("Download failed")
    assert "Failed [######--------------] 3/10 pages" in stream.getvalue()
    assert stream.getvalue().endswith("\n")
    assert progress.ProgressBar.active is None


def test_logs_leave_the_progress_bar_visible():
    stream = Terminal()
    handler = progress.ProgressLoggingHandler(stream)
    with progress.ProgressBar(stream=stream) as update:
        update("Downloading pages", 3, 10)
        handler.handle(logging.LogRecord("test", logging.INFO, "", 0, "Download message", (), None))
    output = stream.getvalue()
    assert "Download message\n" in output
    assert output.rindex("Downloading pages") > output.index("Download message")


@pytest.mark.parametrize("disabled", [False, True])
def test_batch_bar_shows_document_index_and_can_be_disabled(monkeypatch, tmp_path, disabled):
    stream = Terminal()
    source = tmp_path / "links.txt"
    source.write_text("https://www.scribd.com/doc/1\nhttps://www.scribd.com/doc/2\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stderr", stream)
    arguments = ["scribd-downloader", "-f", str(source)]
    if disabled:
        arguments.append("--no-progress")
    monkeypatch.setattr(sys, "argv", arguments)

    async def download(url, *, on_progress, **kwargs):
        if disabled:
            assert on_progress is None
        else:
            assert on_progress is not None
            on_progress("Downloading pages", 0, 3)
            on_progress("Downloading pages", 3, 3)
            on_progress("Assembling PDF", 0, 3)
            on_progress("Assembling PDF", 3, 3)
            on_progress("Saved PDF", 3, 3)
        return 3

    monkeypatch.setattr(cli, "download_pdf", download)
    cli.main()
    if disabled:
        assert stream.getvalue() == ""
    else:
        assert "Document 1/2: Saved PDF" in stream.getvalue()
        assert "Document 2/2: Saved PDF" in stream.getvalue()
        assert stream.getvalue().count("Saved PDF [####################] 3/3 pages") == 2
