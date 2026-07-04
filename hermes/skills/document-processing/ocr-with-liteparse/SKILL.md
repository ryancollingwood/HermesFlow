---
name: ocr-with-liteparse
description: Use the bundled `lit` CLI (liteparse) for OCR and document parsing tasks instead of reaching for tesseract directly
version: 1.0.0
author: Ryan Philip Collingwood
license: MIT
metadata:
  hermes:
    tags: [ocr, pdf, document-parsing, tesseract, liteparse]
    related_skills: []
---

# OCR / Document Parsing: Use `lit` (liteparse)

## When to Use

Any task that needs text out of a PDF, scanned image, or photo of a document
— e.g. "read this invoice", "extract text from this scan", "what does this
PDF say". Reach for `lit` first, not `tesseract` directly.

**Don't use for:** structured JSON extraction against a known schema (no tool
in this stack does that yet — flag it as a gap rather than improvising), or
Office documents (`.docx`/`.odt`) unless LibreOffice is confirmed installed
in this image (it isn't, as of this skill's authoring — check
`hermes/Dockerfile` before assuming otherwise).

## Why `lit`, Not Bare `tesseract`

`liteparse` is baked into the Hermes venv (`hermes/requirements.txt`) and its
`lit` CLI is on `$PATH` (`/opt/hermes/.venv/bin` — see
`docs/hermes-docker-build.md`). It handles PDF rendering (bundled PDFium) and
OCR (via the `tesseract-ocr` apt package baked into `hermes/Dockerfile`) in
one step, with layout/bounding-box output — bare `tesseract` only OCRs
already-rasterized images and can't touch a PDF directly.

## Step-by-Step Workflow

1. Confirm the file is on disk (Hermes' working directory is `/shared` — see
   "Working directory" in `docs/hermes-docker-build.md`).
2. Run:
   ```sh
   lit parse /shared/<path-to-file>
   ```
3. For a directory of files, use `lit batch-parse` instead of looping shell
   calls.
4. If output looks wrong (garbled text, empty result), run
   `lit is-complex /shared/<path-to-file>` first — a "complex" layout (multi-
   column, tables) may need a second pass or manual review; don't silently
   trust a bad first result.

## Known Gotchas

- `lit`'s OCR path depends on the system `tesseract-ocr` package, not a
  bundled binary — if `lit parse` errors specifically on the OCR step (not
  PDF rendering), check `tesseract-ocr` is actually present in this image
  (`which tesseract` inside the container) before assuming a `liteparse` bug.
- Raw image files (PNG/JPG/etc., as opposed to PDFs) go through ImageMagick,
  not PDFium — without the `imagemagick` apt package, `lit parse` on an image
  fails with `"ImageMagick is not installed"` even though PDF parsing works
  fine. Confirmed present in this image; if this error resurfaces, the image
  drifted from `hermes/Dockerfile` — rebuild, don't work around it.
- This is a build-time-baked package (see `docs/hermes-docker-build.md`) —
  it cannot be pip-installed at runtime even if missing. If it's missing,
  that means `hermes/requirements.txt` / `hermes/Dockerfile` drifted from
  what's actually deployed; rebuild (`docker compose build hermes && docker
  compose up -d hermes`) rather than trying to work around it.

> `docs/hermes-docker-build.md` is the canonical doc for how packages get
> baked into this image — if this skill and that doc disagree, the doc wins.
