from argparse import ArgumentParser, ArgumentTypeError
from pathlib import Path


def _concurrency(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise ArgumentTypeError("Concurrency must be an integer between 1 and 256") from None
    if not 1 <= result <= 256:
        raise ArgumentTypeError("Concurrency must be an integer between 1 and 256")
    return result


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Download Scribd page image chunks and assemble them into a PDF")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log download and assembly progress")
    parser.add_argument("-j", "--concurrency", type=_concurrency, default=32,
                        help="Maximum simultaneous HTTP requests (1-256, default: 32)")
    parser.add_argument("-f", "--file", type=Path,
                        help="UTF-8 file containing one Scribd URL per line")
    parser.add_argument("-d", "--output-dir", type=Path,
                        help="Save PDFs in this directory (default: current directory)")
    parser.add_argument("url", nargs="?", help="Scribd /document/<id> or /doc/<id> URL")
    parser.add_argument("output", nargs="?", type=Path,
                        help="Output PDF path (default: <embed-title>.pdf in the current directory)")
    return parser
