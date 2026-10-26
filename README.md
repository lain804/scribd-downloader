# Scribd downloader

Requires Python 3.13 or later.

## Install

With pip:

```sh
python -m pip install git+https://github.com/lain804/scribd-downloader.git
```

With uv:

```sh
uv tool install git+https://github.com/lain804/scribd-downloader.git
```

To install this checkout as a tool and use local fixes immediately, run from
the project directory:

```sh
uv tool install --force --editable .
```

## Usage

```sh
scribd-downloader https://www.scribd.com/document/document-id/some-title
```

By default, the PDF is saved in the current directory using the document title
from Scribd's embed as its filename and PDF title metadata. To choose another
output path, add it after the Scribd URL. Use `-v` to show progress. Pages are
rendered at the maximum available output resolution.

```sh
scribd-downloader [scribd-url]
```

If Scribd presents a browser challenge, the downloader loads the document in
headless Chromium, then downloads the page images directly. The Chromium version
required by the browser backend is installed automatically the first time it is
needed. This may take a few minutes; later runs reuse the installed browser.

## Development

Run the regression tests with `uv run pytest`. They use a local document server
and simulated browser challenges, so they do not require access to Scribd.
