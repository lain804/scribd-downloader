# Scribd downloader

## Installation

Requires Python 3.13+.

```sh
uv tool install git+https://github.com/lain804/scribd-downloader.git
```

Or with pip:

```sh
pip install git+https://github.com/lain804/scribd-downloader.git
```

## Usage

```sh
scribd-downloader <scribd-url>
scribd-downloader --help
```

Page images are embedded losslessly. Documents with native text retain Scribd's
embedded fonts, spacing and layout using headless Chromium (installed automatically
on first use). Scanned OCR overlays are omitted to avoid duplicating the scan.

The downloader accepts up to 10,000 pages per document.
