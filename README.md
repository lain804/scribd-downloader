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
scribd-downloader --file links.txt --output-dir downloads
scribd-downloader --help
```

Page images are embedded losslessly. Documents with native text retain Scribd's
embedded fonts, spacing and layout using headless Chromium (installed automatically
on first use). Scanned OCR overlays are omitted to avoid duplicating the scan.
Links in the text layer appear as styled text.
Browser-backed downloads require a working Chromium sandbox. On Linux, run as
a regular user in an environment that supports Chromium sandboxing.

The downloader accepts up to 10,000 pages per document. The HTTP client allows
the original embed origin and HTTPS URLs on Scribd's domains. It caps decoded
responses at 32 MiB for the embed, 16 MiB for a page chunk or font, and 64 MiB
for an image. Title-based filenames receive a numeric suffix on collisions.
On POSIX, title-based output paths need a filesystem that supports hard links.
An explicit output path replaces the file at that path.

Batch mode works on up to four documents at once and reports progress by
document. The batch shares one HTTP connection pool and one request limit.
Use `-j` to set that limit (default 32).

HTTP 429 and HTTP 503 with a valid `Retry-After` pause requests to the responding
origin while other origins continue. The downloader retries up to three times.
A 429 without a usable header uses exponential backoff with jitter. If the
server asks for more than 60 seconds, the download reports the error without
retrying early.
