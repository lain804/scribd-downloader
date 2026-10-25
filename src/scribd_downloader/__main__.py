import asyncio
import logging
import re

from scribd_downloader.cli import build_parser
from scribd_downloader.constants import DOCUMENT_ID_SEARCH_PATTERN
from scribd_downloader.pages import download_pdf


def extract_document_id(url: str):
    match = re.fullmatch(DOCUMENT_ID_SEARCH_PATTERN, url)
    return match.group(1) if match else None


def main():
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="[%(asctime)s] [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    document_id = extract_document_id(args.url)
    if document_id is None:
        parser.error("Expected a Scribd /document/<id> or /doc/<id> URL")
    try:
        count = asyncio.run(download_pdf(
            f"https://www.scribd.com/embeds/{document_id}/content", args.output,
        ))
    except Exception as exc:
        logging.error("Download failed: %s", exc)
        raise SystemExit(1) from exc
    destination = args.output if args.output is not None else "PDF in current directory"
    print(f"Saved {destination} ({count} pages)")


if __name__ == "__main__":
    main()
