# Ingestion

`kb_ingest` (MCP, up to 10 MB, text or base64) and `hive ingest PATH... --org O --user U [--project P] [--tag T]`
(CLI, directories recursively, up to 50 MB per file) add documents as searchable, linked knowledge.

- **Formats:** text and source code, markdown, HTML (scripts/styles dropped, headings kept) and PDF
  (needs `pip install hive-mcp[documents]`; encrypted PDFs are refused). Binary or unknown types are refused
  with a message; nothing is stored for a refused file.
- **Parts:** text longer than ~40 KB is split at headings, then paragraphs, into items titled `name (i/n)`.
  Part `i` is linked `part_of` part 1 and part `i-1` is linked `next` to part `i` (see GRAPH.md).
  A document may have at most 200 parts.
- **Private by default**; `--project` / `share_with_project` shares into a project the user belongs to.
  Secrets found in the text are masked before storing, like any other item.
- **Idempotent:** the same bytes with the same name, tags and scope store nothing new, so a failed bulk run
  can simply be repeated. Changing any of them stores new items (old ones are kept; archive them with `kb_update`).
- Each item records `source = file:<name>`; the original file is not kept.

Not done yet: automatic entity extraction (the server has no LLM; agents add entities and `kb_link`s),
Office formats, OCR, and ingestion from URLs.
