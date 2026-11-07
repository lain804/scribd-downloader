# PDF pipeline investigation

The previous pipeline unnecessarily enlarged every page to 3,000 pixels wide,
saved it as a lossless PNG, converted the PNGs to PDF, and rewrote that PDF to add
text. This discarded source JPEG compression and duplicated shared source images
across pages. Upscaling added interpolated pixels rather than source detail.

The replacement embeds each original image once and uses PDF clipping commands
to reconstruct the page. Compatible JPEG/PNG streams retain their compression,
resolution and color information. The text layer is created once for the whole
document. Other image formats still need lossless conversion; JPEG/PNG are now
preferred in HTTP image negotiation to avoid that expense where supported.

## Measured assembly results

Run from the repository root:

```sh
uv run python benchmarks/pdf_pipeline.py --baseline-ref 07964da --repeats 3
```

Measured on an Intel Core i5-12450H, Linux, Python 3.13.18. Each fixture has 12
distinct 1,200 × 1,600 source images containing text and a seeded 600 × 400 random
noise block. Each page uses eight clips from its source image. These synthetic
fixtures exercise compression and clipping; their sizes do not describe every
real Scribd document. Each measurement runs in a fresh subprocess. Times and RSS
are medians of three runs. Imports and fixture generation are excluded from the
assembly timer but included in peak RSS.

| Source | Previous assembly | Current assembly | Previous PDF | Current PDF | Previous/current peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| JPEG | 7.336 s | 0.025 s | 57.465 MiB | 4.262 MiB | 410.8 / 81.6 MiB |
| PNG | 6.098 s | 0.036 s | 47.562 MiB | 9.030 MiB | 443.5 / 82.1 MiB |
| WebP | 7.090 s | 2.094 s | 53.387 MiB | 11.264 MiB | 464.6 / 111.9 MiB |

JPEG/PNG assembly was approximately 169–293 times faster in this fixture, with
81–93% smaller PDFs. WebP was 3.4 times faster and 79% smaller, but its required
lossless transcode remains expensive. No additional lossy compression is applied.
Output size depends on source encoding and how much of each source image is used;
direct embedding retains the entire source asset even when only part is visible.

## Download behavior

- Images are queued as soon as inline or fetched page data is parsed, overlapping
  image requests with remaining page-description requests.
- A shared curl connection pool limits all HTTP requests. The default increased
  from 16 to 32 and can be set with `--concurrency`.
- Fixed worker counts replace one asyncio task per page/image. Downloads are
  spooled to temporary files, limiting simultaneous response buffers to the worker
  count; assembly still retains encoded image streams internally.
- Inline page parsing uses individual page nodes, avoiding repeated full-document
  XPath searches. Relative chunk URLs are resolved before requesting them.
- Failures cancel and await sibling tasks before closing the session and removing
  temporary files. Final output replacement remains atomic.

Local regression tests verify that an image can finish while a page chunk is
still pending, out-of-order chunks retain PDF page order, each image URL is
downloaded once, and chunk/image requests share the configured connection limit.
Live Scribd/CDN speed and challenge latency were not benchmarked. Server limits,
available bandwidth, and browser challenges can dominate total download time.

## Correctness findings

Live embed inspection found that text layers can contain visible content, and
some pages have only HTML text. The old parser rejected those pages. They are now
supported; visible text is retained, while explicitly transparent or zero-opacity
text remains invisible and searchable. A span's own font size is recognized,
alpha transparency is preserved,
clipping is intersected correctly with image/page boundaries, and oversized image
headers are rejected before allocating pixel buffers. Long generated filenames
are limited by UTF-8 byte length to fit ordinary filesystem filename limits.

Regression checks cover byte-for-byte JPEG preservation, shared PDF image
objects, lossless PNG modes, searchability, visual clipping/transparency through
Poppler, and preservation of an existing output on download/assembly failure.
HTML text still has the existing Helvetica/Windows-1252, custom font mapping and
fixed text-transform limitations described in the main README.

Live sample: the [MIT license embed](https://www.scribd.com/embeds/595393746/content)
contains visible text without image segments. This was used to check text-only
page handling; live transfer timings are not included in the benchmark table.
