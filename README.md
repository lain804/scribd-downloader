# Scribd downloader

Requires Python 3.13 or later. Run these commands from the project directory.

## Install

With pip:

```sh
python -m pip install .
```

With uv:

```sh
uv tool install .
```

## Usage

```sh
scribd-downloader https://www.scribd.com/document/55026066/Some-Title
```

By default, the PDF is saved to `output/pdf/<document-id>.pdf`. Use `-o` to
choose a path, `-v` to show progress, or `--scale 1`, `--scale 2`, or
`--scale 3` to set the output resolution (default: `2`).

```sh
scribd-downloader -o document.pdf --scale 3 <scribd-url>
```
