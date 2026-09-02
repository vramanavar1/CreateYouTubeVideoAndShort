# Thumbnail font

Drop a bold TrueType face here named `title.ttf` and every thumbnail will use it.

The renderer falls back, in order, to:

1. `assets/fonts/title.ttf` — this folder
2. Segoe UI Bold / Arial Bold (Windows)
3. DejaVu Sans Bold (Linux)
4. Pillow's built-in scalable default

Bundling a font makes output byte-identical across machines, which matters if you
ever diff rendered thumbnails. Pick one with a licence that permits embedding and
redistribution — the [Open Font License](https://openfontlicense.org/) faces on
Google Fonts (Inter, Archivo, Anton) are all fine.

Font files are not committed by default; add an exception in `.gitignore` if you
want yours tracked.
