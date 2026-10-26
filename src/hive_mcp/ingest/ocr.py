"""Text from scanned documents and images, by running Tesseract (and Poppler's pdftoppm for PDFs).

Both are C programs that read untrusted files, so they run as separate processes with a time limit
per call, a memory limit, a page cap and a total deadline, and their output is only ever read as text.
In the container they run as the unprivileged user inside the container's own limits. Nothing is
installed by default: enable with HIVE_OCR=true on an image built with --build-arg WITH_OCR=true.
"""

from __future__ import annotations

import resource
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"})
MIN_CHARS_PER_PAGE = 30  # a PDF page with less text than this is treated as a scan
MEMORY_LIMIT = 2 * 1024**3
PAGE_SECONDS = 60
RENDER_DPI = 200


@dataclass(frozen=True, slots=True)
class OcrConfig:
    languages: str = "eng"  # Tesseract codes joined with "+", e.g. "eng+deu"
    max_pages: int = 50
    deadline: float = 300.0  # seconds for one document


def check_available(need_pdf: bool = True) -> None:
    """Fail early, with the fix, when OCR is switched on but the programs are not installed."""
    missing = [n for n in ("tesseract", *(("pdftoppm",) if need_pdf else ())) if not shutil.which(n)]
    if missing:
        raise ValidationError(
            f"HIVE_OCR is on but {', '.join(missing)} is not installed "
            "(build the image with --build-arg WITH_OCR=true, or install tesseract-ocr and poppler-utils)"
        )


def _limit_memory() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


def _run(cmd: list[str], data: bytes | None, timeout: float) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv; the file content goes on stdin or in a temp file
            cmd, input=data, capture_output=True, timeout=timeout, check=False,
            preexec_fn=_limit_memory,  # noqa: PLW1509 - only calls setrlimit
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"{cmd[0]} took longer than {timeout:.0f} seconds") from exc
    except OSError as exc:
        raise ValidationError(f"cannot run {cmd[0]}: {exc.strerror}") from exc
    if result.returncode != 0:
        raise ValidationError(f"{cmd[0]} could not read this file")
    return result.stdout


def _tesseract(image: bytes, config: OcrConfig, timeout: float) -> str:
    out = _run(["tesseract", "stdin", "stdout", "-l", config.languages], image, timeout)
    return out.decode("utf-8", errors="replace").replace("\f", "\n").strip()


def ocr_image(data: bytes, config: OcrConfig) -> str:
    return _tesseract(data, config, PAGE_SECONDS)


def ocr_pdf_pages(
    data: bytes, pages: list[int], config: OcrConfig, clock: Callable[[], float] = time.monotonic
) -> dict[int, str]:
    """Text of the given 1-based pages, rendered with pdftoppm then read by Tesseract."""
    if len(pages) > config.max_pages:
        raise ValidationError(
            f"{len(pages)} pages need OCR, more than the limit of {config.max_pages}; split the file"
        )
    started, found = clock(), {}
    with tempfile.TemporaryDirectory(prefix="hive-ocr-") as tmp:
        source = Path(tmp) / "in.pdf"
        source.write_bytes(data)
        for page in pages:
            remaining = config.deadline - (clock() - started)
            if remaining <= 0:
                raise ValidationError(f"OCR took longer than {config.deadline:.0f} seconds")
            timeout = min(PAGE_SECONDS, remaining)
            png = _run(
                ["pdftoppm", "-r", str(RENDER_DPI), "-f", str(page), "-l", str(page), "-png",
                 "-singlefile", str(source), str(Path(tmp) / "page")],
                None, timeout,
            )  # fmt: skip
            del png  # pdftoppm writes page.png next to the input
            image = (Path(tmp) / "page.png").read_bytes()
            (Path(tmp) / "page.png").unlink()
            found[page] = _tesseract(image, config, timeout)
    return found
