import asyncio
import logging
from pathlib import Path
import sys

import pytest

from scribd_downloader import __main__ as cli


@pytest.mark.parametrize("jobs,expected_peak", [(None, 4), (1, 1), (6, 6), (8, 6)])
def test_batch_jobs_sets_document_concurrency(monkeypatch, tmp_path, jobs, expected_peak):
    source = tmp_path / "links.txt"
    source.write_text("".join(f"https://www.scribd.com/doc/{number}\n" for number in range(1, 7)))
    active = peak = 0

    async def download(session, url, **kwargs):
        nonlocal active, peak
        assert session.max_clients == 2
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return 1

    arguments = ["scribd-downloader", "--file", str(source), "-j", "2", "-d", str(tmp_path)]
    if jobs is not None:
        arguments += ["--jobs", str(jobs)]
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(cli, "_download_pdf", download)
    cli.main()
    assert peak == expected_peak


@pytest.mark.parametrize("arguments,expected_error", [
    (["--file", "links.txt", "--jobs", "0"], "--jobs must be a positive integer"),
    (["--file", "links.txt", "--jobs", "-1"], "--jobs must be a positive integer"),
    (["--file", "links.txt", "--jobs", "1.5"], "invalid int value"),
    (["https://www.scribd.com/doc/1", "--jobs", "4"], "--jobs requires --file"),
])
def test_invalid_batch_jobs_is_rejected(monkeypatch, capsys, arguments, expected_error):
    monkeypatch.setattr(sys, "argv", ["scribd-downloader", *arguments])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert expected_error in capsys.readouterr().err


@pytest.mark.parametrize("directory", [None, "downloads"])
def test_batch_reads_bom_skips_blanks_and_deduplicates_documents(monkeypatch, tmp_path, capsys, directory):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "links.txt"
    source.write_text(
        "\ufeff  https://www.scribd.com/document/123/First  \n\n"
        "https://www.scribd.com/doc/456/Second\n"
        "HTTPS://WWW.SCRIBD.COM/doc/00123/Another-title?share=1\n",
        encoding="utf-8",
    )
    calls = []

    async def download(session, url, **kwargs):
        calls.append((session, url, kwargs))
        return 2

    monkeypatch.setattr(cli, "_download_pdf", download)
    arguments = ["scribd-downloader", "--file", str(source), "-j", "8"]
    if directory:
        arguments.extend(["--output-dir", directory])
    monkeypatch.setattr(sys, "argv", arguments)
    cli.main()

    assert [url for _, url, _ in calls] == ["https://www.scribd.com/embeds/123/content",
                                           "https://www.scribd.com/embeds/456/content"]
    for session, _, kwargs in calls:
        assert session is calls[0][0]
        assert session.max_clients == 8
        assert kwargs == {"output_dir": Path(directory) if directory else tmp_path,
                          "overwrite": False}
    output = capsys.readouterr().out
    assert "Saved 2 documents" in output
    assert "0 failed; 1 duplicates skipped" in output


def test_batch_continues_after_invalid_links_and_failed_downloads(monkeypatch, tmp_path, capsys, caplog):
    source = tmp_path / "links.txt"
    source.write_text("https://www.scribd.com/doc/1\ninvalid\n"
                      "https://www.scribd.com/doc/2\nhttps://www.scribd.com/doc/3\n")
    calls = []

    async def download(session, url, **kwargs):
        calls.append(url)
        if url.endswith("/2/content"):
            raise RuntimeError("Server unavailable")
        return 1

    monkeypatch.setattr(cli, "_download_pdf", download)
    monkeypatch.setattr(sys, "argv", ["scribd-downloader", "-f", str(source)])
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert calls == [f"https://www.scribd.com/embeds/{number}/content" for number in (1, 2, 3)]
    assert f"{source}:2:" in caplog.text
    assert "Server unavailable" in caplog.text
    assert "Saved 2 documents" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://www.scribd.com/doc/١٢٣", "https://www.scribd.com/doc/123/title\x1b"])
def test_document_url_rejects_non_ascii_ids_and_terminal_controls(url):
    assert cli.extract_document_id(url) is None


@pytest.mark.parametrize("contents", [None, b"\n \n", b"\xff"])
def test_unreadable_or_empty_batch_is_reported_before_downloading(monkeypatch, tmp_path, contents, capsys):
    source = tmp_path / "links.txt"
    if contents is not None:
        source.write_bytes(contents)

    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must be rejected before downloading")

    monkeypatch.setattr(cli, "_download_pdf", unexpected)
    monkeypatch.setattr(sys, "argv", ["scribd-downloader", "-f", str(source)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [[], ["-f", "links.txt", "https://www.scribd.com/doc/1"],
                                        ["https://www.scribd.com/doc/1", "out.pdf", "-d", "downloads"]])
def test_conflicting_or_missing_input_is_rejected(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["scribd-downloader", *arguments])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_batch_rejects_output_file_as_directory_before_downloading(monkeypatch, tmp_path, capsys):
    source = tmp_path / "links.txt"
    source.write_text("https://www.scribd.com/doc/123\n")
    destination = tmp_path / "existing"
    destination.write_bytes(b"existing file")

    async def unexpected(*args, **kwargs):
        pytest.fail("An invalid directory must be rejected before downloading")

    monkeypatch.setattr(cli, "_download_pdf", unexpected)
    monkeypatch.setattr(sys, "argv", ["scribd-downloader", "-f", str(source), "-d", str(destination)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Could not create output directory" in capsys.readouterr().err
    assert destination.read_bytes() == b"existing file"


@pytest.mark.parametrize("arguments,expected_output,expected_dir", [
    (["https://www.scribd.com/doc/123", "manual.pdf"], Path("manual.pdf"), None),
    (["https://www.scribd.com/doc/123", "-d", "downloads"], None, Path("downloads")),
])
def test_single_url_output_remains_supported(monkeypatch, arguments, expected_output, expected_dir):
    calls = []

    async def download(url, output, **kwargs):
        calls.append((url, output, kwargs))
        return 2

    monkeypatch.setattr(cli, "download_pdf", download)
    monkeypatch.setattr(sys, "argv", ["scribd-downloader", *arguments])
    cli.main()
    assert calls == [("https://www.scribd.com/embeds/123/content", expected_output,
                      {"concurrency": 32, "output_dir": expected_dir,
                       "overwrite": expected_output is not None, "on_progress": None})]
