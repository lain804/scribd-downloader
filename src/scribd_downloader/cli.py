from argparse import ArgumentParser
from pathlib import Path


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Download Scribd page image chunks and assemble them into a PDF")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log download and assembly progress")
    parser.add_argument("-o", "--output", type=Path, help="Output PDF path (default: output/pdf/<document-id>.pdf)")
    parser.add_argument("--scale", type=int, choices=(1, 2, 3), default=2,
                        help="Output scale at 96 DPI (default: 2; page width is 750 pt)")
    parser.add_argument("url", help="Scribd /document/<id> or /doc/<id> URL")
    return parser
