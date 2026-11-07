import asyncio
import logging
from pathlib import Path
import re

from scribd_downloader.cli import build_parser
from scribd_downloader.constants import DOCUMENT_ID_SEARCH_PATTERN
from scribd_downloader.pages import download_pdf
from scribd_downloader.progress import ProgressBar, ProgressLoggingHandler


def extract_document_id(url: str):
    match = re.fullmatch(DOCUMENT_ID_SEARCH_PATTERN, url)
    return match.group(1) if match else None


def _read_batch(path: Path):
    documents = []
    seen = set()
    invalid = 0
    duplicates = 0
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        url = line.strip()
        if not url:
            continue
        document_id = extract_document_id(url)
        if document_id is None:
            logging.error("%s:%d: Invalid Scribd document URL: %s", path, number, url)
            invalid += 1
        elif document_id in seen:
            duplicates += 1
        else:
            seen.add(document_id)
            documents.append((url, document_id))
    return documents, invalid, duplicates


async def _download_batch(documents, output_dir: Path, concurrency: int, show_progress: bool = True):
    saved = 0
    failed = 0
    for number, (url, document_id) in enumerate(documents, 1):
        try:
            with ProgressBar(f"Document {number}/{len(documents)}", enabled=show_progress) as on_progress:
                count = await download_pdf(
                    f"https://www.scribd.com/embeds/{document_id}/content",
                    output_dir=output_dir, overwrite=False, concurrency=concurrency,
                    on_progress=on_progress,
                )
        except Exception as exc:
            logging.error("Download failed for %s: %s", url, exc)
            failed += 1
        else:
            print(f"Saved {url} ({count} pages)")
            saved += 1
    return saved, failed


def main():
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        handlers=[ProgressLoggingHandler()],
        level=logging.INFO if args.verbose else logging.WARNING,
        format="[%(asctime)s] [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if (args.url is None) == (args.file is None):
        parser.error("Provide either a Scribd URL or --file LINKS_FILE")
    if args.output is not None and args.output_dir is not None:
        parser.error("Choose either an output PDF path or --output-dir")
    if args.file is not None:
        try:
            documents, invalid, duplicates = _read_batch(args.file)
        except (OSError, UnicodeError) as exc:
            parser.error(f"Could not read links file {args.file}: {exc}")
        if not documents:
            parser.error(f"No valid Scribd document URLs found in {args.file}")
        output_dir = args.output_dir if args.output_dir is not None else Path.cwd()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            parser.error(f"Could not create output directory {output_dir}: {exc}")
        saved, failed = asyncio.run(_download_batch(
            documents, output_dir, args.concurrency, show_progress=not args.no_progress,
        ))
        failed += invalid
        print(f"Saved {saved} documents to {output_dir}; {failed} failed; {duplicates} duplicates skipped")
        if failed:
            raise SystemExit(1)
        return
    document_id = extract_document_id(args.url)
    if document_id is None:
        parser.error("Expected a Scribd /document/<id> or /doc/<id> URL")
    try:
        with ProgressBar(enabled=not args.no_progress) as on_progress:
            count = asyncio.run(download_pdf(
                f"https://www.scribd.com/embeds/{document_id}/content", args.output,
                concurrency=args.concurrency, output_dir=args.output_dir,
                on_progress=on_progress,
            ))
    except Exception as exc:
        logging.error("Download failed: %s", exc)
        raise SystemExit(1) from exc
    destination = args.output if args.output is not None else f"PDF in {args.output_dir or Path.cwd()}"
    print(f"Saved {destination} ({count} pages)")


if __name__ == "__main__":
    main()
