"""Reproducible offline PDF assembly benchmark, with optional historical comparison.

uv run python benchmarks/pdf_pipeline.py --baseline-ref 07964da --repeats 3

Each measurement runs in a fresh process. Fixture generation and imports are
excluded from the timer; peak RSS includes them. The fixtures mix text and a
seeded noise block, with eight clipped placements per page.
They are synthetic, not a measurement of Scribd network/challenge latency.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import random
import resource
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import types

import img2pdf
from PIL import Image, ImageDraw

from scribd_downloader import pages


def fixture(directory, format, count):
    assets = {}
    document = []
    rng = random.Random(42)
    for number in range(1, count + 1):
        with Image.new("RGB", (1200, 1600), "white") as image:
            draw = ImageDraw.Draw(image)
            for row in range(12):
                draw.text((80, 60 + row * 35), f"Document page {number}: text, tables and images. " * 3,
                          fill="black", font_size=20)
            with Image.frombytes("RGB", (600, 400), rng.randbytes(600 * 400 * 3)) as noise:
                image.paste(noise, (300, 650))
            buffer = BytesIO()
            image.save(buffer, format=format, quality=85)
        url = f"asset-{number}"
        path = directory / url
        path.write_bytes(buffer.getvalue())
        assets[url] = path
        # All eight clips reuse the same source, matching Scribd's image atlases.
        placements = tuple(
            pages.Placement(url, 0, 0, 1200, 1600, (top, 1200, top + 200, 0))
            for top in range(0, 1600, 200)
        )
        text = ((400, 300, 100, (0, 0, 0), f"Page {number} visible text"),)
        document.append(pages.Page(number, 1200, 1600, placements, text))
    return document, assets


def baseline_module(ref):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:src/scribd_downloader/pages.py"], text=True,
    )
    name = "scribd_downloader._benchmark_baseline"
    module = types.ModuleType(name)
    module.__package__ = "scribd_downloader"
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def measure(args):
    module = baseline_module(args.baseline_ref) if args.worker == "baseline" else pages
    with TemporaryDirectory(prefix="scribd-benchmark-") as temporary:
        directory = Path(temporary)
        document, asset_paths = fixture(directory, args.format, args.pages)
        started = perf_counter()
        if args.worker == "baseline":
            assets = {url: path.read_bytes() for url, path in asset_paths.items()}
            jobs = [(page, assets, directory / f"page-{page.number}.png") for page in document]
            with ThreadPoolExecutor(max_workers=min(4, args.pages)) as executor:
                paths = list(executor.map(lambda job: module._render_and_save(*job), jobs))
            output = directory / "document.pdf"
            with output.open("wb") as stream:
                img2pdf.convert(
                    [str(path) for path in paths], outputstream=stream, title="Benchmark",
                    layout_fun=img2pdf.get_fixed_dpi_layout_fun((288, 288)),
                )
            module._merge_searchable_text(output, document, "Benchmark")
        else:
            output = pages._assemble_pdf(document, asset_paths, directory, "Benchmark")
        elapsed = perf_counter() - started
        print(json.dumps({"seconds": elapsed, "bytes": output.stat().st_size,
                          "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--pages", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--worker", choices=["baseline", "current"], help="Internal subprocess mode")
    parser.add_argument("--format", choices=["JPEG", "PNG", "WEBP"])
    args = parser.parse_args()
    if args.pages < 1 or args.repeats < 1:
        parser.error("Pages and repeats must be positive")
    if args.worker:
        measure(args)
        return
    print("Format  Version    Seconds   PDF MiB   Peak RSS MiB", flush=True)
    for format in ("JPEG", "PNG", "WEBP"):
        for version in (("baseline", "current") if args.baseline_ref else ("current",)):
            results = []
            for _ in range(args.repeats):
                command = [sys.executable, __file__, "--worker", version,
                           "--format", format, "--pages", str(args.pages)]
                if args.baseline_ref:
                    command.extend(["--baseline-ref", args.baseline_ref])
                results.append(json.loads(subprocess.check_output(command, text=True)))
            seconds = median(result["seconds"] for result in results)
            size = median(result["bytes"] for result in results) / 1024**2
            rss = median(result["peak_rss_mib"] for result in results)
            print(f"{format:6}  {version:8}  {seconds:7.3f}  {size:8.3f}  {rss:13.1f}", flush=True)


if __name__ == "__main__":
    main()
