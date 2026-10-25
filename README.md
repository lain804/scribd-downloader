# Scribd downloader

Download Scribd's page metadata and image atlases with asynchronous HTTP
requests, then reconstruct the visible page layouts into a PDF with a searchable
text layer. The downloader does not launch a browser.

```powershell
uv sync
uv run scribd-downloader https://www.scribd.com/document/55026066/Some-Title
```

The default output is `output/pdf/<document-id>.pdf`. Use `-o filename.pdf` to
choose an output path, `-v` for progress, and `--scale 3` for a larger rendered
page image. Scale defaults to 2. It controls output pixels; page width remains
750 PDF points.

For each document, the downloader reads inline pages and page metadata from the
embed HTML, fetches missing JSONP page chunks concurrently, and downloads each
unique image atlas once. It reconstructs each white page canvas by applying the
image elements' CSS positions and `clip: rect(...)` regions in document order.
Page output is rendered at the browser viewer's 1000 CSS-pixel width and device
scale, then assembled into a PDF. Scribd's text layer is rendered on top of the
page images and remains searchable and selectable. Missing, blurred, or
incomplete pages fail instead of producing a partial output.

Scribd may return a JavaScript client challenge to direct HTTP requests. The
downloader detects that response and reports it; it does not switch to browser
automation. This currently prevents fresh manifest retrieval from some networks.

```text
scribd-downloader [-h] [-v] [-o OUTPUT] [--scale {1,2,3}] url
```
