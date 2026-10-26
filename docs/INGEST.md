# Ingestion

`kb_ingest` (MCP, up to 10 MB, text or base64) and `hive ingest PATH... --org O --user U [--project P] [--tag T]`
(CLI, directories recursively, up to 50 MB per file) add documents as searchable, linked knowledge.

- **Formats:** text and source code, markdown, HTML (scripts/styles dropped, headings kept), Word `.docx`
  (headings, paragraphs, tables), Excel `.xlsx` (every sheet in workbook order, rows as `a | b | c`, capped at
  20,000 rows per sheet), PowerPoint `.pptx` (slides, titles, speaker notes) and PDF (needs
  `pip install hive-mcp[documents]`; encrypted PDFs are refused). The Office readers use only the standard
  library and refuse zip/XML bombs (size caps, DTDs). Old binary `.doc/.xls/.ppt`, images and scans are refused with
  a message (no OCR: text in images is not read); nothing is stored for a refused file.
- **Parts:** text longer than ~40 KB is split at headings, then paragraphs, into items titled `name (i/n)`.
  Part `i` is linked `part_of` part 1 and part `i-1` is linked `next` to part `i` (see GRAPH.md).
  A document may have at most 200 parts.
- **Private by default**; `--project` / `share_with_project` shares into a project the user belongs to.
  Secrets found in the text are masked before storing, like any other item.
- **Idempotent:** the same bytes with the same name, tags and scope store nothing new, so a failed bulk run
  can simply be repeated. Changing any of them stores new items (old ones are kept; archive them with `kb_update`).
- Each item records `source = file:<name>`; the original file is not kept.

## From a URL: `kb_ingest_url`
Off by default (`HIVE_INGEST_URLS_ENABLED=true`) because a server that fetches addresses for its users can be
aimed at things only the server can reach. Safeguards on every hop of every redirect: http(s) only, no credentials in
the URL, the name is resolved by the server and **every** address must be public (private, loopback, link-local
such as the cloud metadata address, CGNAT and multicast ranges are refused; `HIVE_INGEST_URL_ALLOW_PRIVATE=true`
opts in for an intranet), the connection goes to the address that was checked (a DNS answer that changes between check
and connect cannot redirect it; TLS still verifies the real host name), 15 s and 20 MB limits, documents only
(no images/video). `HIVE_INGEST_URL_HOSTS=["docs.acme.com"]` restricts to a list (subdomains included). It
connects directly, ignoring proxy environment variables, so the egress rules of your network apply.
The stored item's `source` is `url:<final address>`.

## From a Git repository: `hive ingest-git URL`
Operator command (never an agent tool): shallow-clones (`--depth 1`, no submodules, no hooks run, ext:: transport
off), reads documents, markdown, code and Office files under 1 MB, skips binaries, `node_modules`, `vendor`,
`dist`, `build`, lock files, minified/generated files (`--exclude GLOB` adds more), tags everything `repo/<name>`
and records `git:<url>#<path>` as the source. Safe to re-run. Needs `git` (Docker build arg `WITH_GIT=true`).

Not done: automatic entity *discovery* (the server has no LLM; agents add entities), OCR, legacy Office formats.


## Scanned documents and images (OCR)
Off by default. With `HIVE_OCR=true`, `kb_ingest`, `hive ingest` and `kb_ingest_url` also read **images** (`.png .jpg .jpeg .tif
.tiff .bmp .gif .webp`) and **PDFs without a text layer** with [Tesseract](https://github.com/tesseract-ocr/tesseract); Poppler's
`pdftoppm` renders the PDF pages. Results carry the tag `format/image-ocr` or `format/pdf-ocr`; OCR text is only as good as the
scan, so check important numbers against the original.

| Setting | Meaning |
|---|---|
| `HIVE_OCR` | `true` to enable; the server refuses to start if `tesseract` or `pdftoppm` is missing |
| `HIVE_OCR_LANGUAGES` | Tesseract language codes joined with `+` (default `eng`, e.g. `eng+deu`); the packs must be installed |
| `HIVE_OCR_MAX_PAGES` | most pages OCR will read in one document (default 50) |

Docker: `docker build --build-arg WITH_OCR=true --build-arg OCR_LANGS="deu fra" -t hive-mcp .` (about 80 MB more).
Elsewhere: `apt install tesseract-ocr poppler-utils`.

How it works: a PDF page with fewer than 30 characters of text is treated as a scan, so documents that mix text pages and scans
work; a text document that merely has a few blank pages more than the limit is left alone; if more than the limit of pages (or
more than half the pages) need OCR the file is **refused** with a message to split it, never silently cut. Without OCR an image
or a scanned PDF is refused with a message that says how to enable it. OCR is slow (seconds per page): it runs in a worker
thread, so other requests are not held up, but the call itself waits; the limits are 60 s per page and 300 s per document.

Safety: Tesseract and Poppler are C programs reading untrusted files, so each call runs as a separate process with a time limit
and a 2 GB memory limit, as the container's unprivileged user. Nothing from the file reaches a command line (languages are
validated, files go through stdin or a private temporary directory).
