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

If Scribd presents a browser challenge, the downloader automatically installs
Scrapling's headless browser dependencies the first time they are needed. This
may take a few minutes; later runs reuse the installed browsers.
