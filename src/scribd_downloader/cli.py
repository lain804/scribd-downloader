from argparse import ArgumentParser
from pathlib import Path


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Download Scribd page image chunks and assemble them into a PDF")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log download and assembly progress")
    parser.add_argument("url", help="Scribd /document/<id> or /doc/<id> URL")
    parser.add_argument("output", nargs="?", type=Path,
                        help="Output PDF path (default: <embed-title>.pdf in the current directory)")
    return parser
