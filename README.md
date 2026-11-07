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
output path, add it after the Scribd URL. Use `-v` to show download time, assembly
time and final file size. Pages retain their source image resolution; JPEG and
compatible PNG data are embedded directly without enlargement or lossy recompression.
Shared image chunks are stored once in the PDF, with their original clipping.

```sh
scribd-downloader [scribd-url]
```

Downloads use up to 32 simultaneous requests by default. Page images start
downloading as their page descriptions arrive. Change the shared request limit
with `-j` / `--concurrency` (1–256):

```sh
scribd-downloader -v -j 64 https://www.scribd.com/document/document-id/some-title output.pdf
```

JPEG and PNG are preferred during image format negotiation because PDF supports
them directly. Other supported image formats are converted losslessly, which can
take longer and produce larger files. Temporary source images are removed after
completion or failure, and an existing output is replaced only after successful
assembly.

If Scribd presents a browser challenge, the downloader loads the document in
headless Chromium, then downloads the page images directly. The Chromium version
required by the browser backend is installed automatically the first time it is
needed. This may take a few minutes; later runs reuse the installed browser.

## Development

Run the regression tests with `uv run pytest`. They use a local document server
and simulated browser challenges, so they do not require access to Scribd.

For reproducible offline assembly benchmarks and measured results, see
[benchmarks/README.md](benchmarks/README.md).

HTML text is preserved on mixed and text-only pages. Explicitly transparent text
remains invisible and searchable. Text uses Helvetica with Windows-1252 encoding;
other characters are replaced with `?`, and custom Scribd font mappings are not
reconstructed. Image-backed text retains its original appearance. Text
positioning assumes Scribd's usual `scale(.2)` overlay. Blurred pages and
canvas/SVG content are reported as unsupported rather than silently omitted.
