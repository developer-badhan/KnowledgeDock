"""Text extraction.

One interface, five implementations. Each returns plain text and is allowed to
fail: a PDF with no extractable text raises `ExtractionError`, which the use case
turns into a `FAILED` document with a diagnosable reason rather than an empty
knowledge base entry.

Why these libraries (`SKILL.md` §34): the standard library cannot read PDF or
DOCX, so the capability has to come from a package. `pypdf` and `python-docx` are
the maintained, vendor-neutral choices. HTML is parsed with `html.parser` from
the standard library — no dependency needed for stripping tags, and it does not
execute anything, which matters because an uploaded HTML file is untrusted input.
"""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO

from knowledgedock.infrastructure.storage import CONTENT_TYPE_EXTENSIONS


class ExtractionError(RuntimeError):
    """No usable text could be recovered. The reason is safe to store."""


class TextExtractor(ABC):
    @abstractmethod
    def extract(self, source: BinaryIO) -> str:
        """Return the document's text, or raise `ExtractionError`."""

    @abstractmethod
    def handles(self, content_type: str) -> bool: ...


class PlainTextExtractor(TextExtractor):
    """Handles `.txt` and `.md`. Markdown is read as-is; chunking sees the syntax.

    No markdown rendering: the retriever indexes what a person would search for,
    and stripping syntax would only lose terms.
    """

    def extract(self, source: BinaryIO) -> str:
        raw = source.read()
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        # latin-1 never fails, so this is unreachable in practice. Kept so a
        # future encoding change cannot silently truncate a document.
        raise ExtractionError("Could not decode the file as text.")  # pragma: no cover

    def handles(self, content_type: str) -> bool:
        return content_type in ("text/plain", "text/markdown")


class _HTMLTextCollector(HTMLParser):
    """Collects text, dropping script, style and anything else non-visible.

    Written as an explicit parser rather than a regex so malformed markup cannot
    desynchronise the output, and so `<script>` bodies are never indexed.
    """

    SKIP = {"script", "style", "noscript", "template", "head", "svg"}
    BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


class HtmlTextExtractor(TextExtractor):
    def extract(self, source: BinaryIO) -> str:
        raw = source.read()
        try:
            markup = raw.decode("utf-8")
        except UnicodeDecodeError:
            markup = raw.decode("latin-1", errors="replace")
        parser = _HTMLTextCollector()
        try:
            parser.feed(markup)
            parser.close()
        except Exception as exc:
            # html.parser is forgiving, but a pathological file can still raise.
            raise ExtractionError(f"Could not parse the HTML: {type(exc).__name__}") from exc
        text = html.unescape(parser.text())
        if not text.strip():
            raise ExtractionError("The HTML file contains no visible text.")
        return text

    def handles(self, content_type: str) -> bool:
        return content_type == "text/html"


class PdfTextExtractor(TextExtractor):
    def extract(self, source: BinaryIO) -> str:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(source)
            pages = [(page.extract_text() or "") for page in reader.pages]
        except PdfReadError as exc:
            raise ExtractionError("The file is not a readable PDF.") from exc
        except Exception as exc:
            raise ExtractionError(f"PDF extraction failed: {type(exc).__name__}") from exc

        text = "\n\n".join(pages).strip()
        if not text:
            # The common case: a scanned PDF, where the text is images.
            raise ExtractionError("No text could be extracted. The PDF is probably scanned images.")
        return text

    def handles(self, content_type: str) -> bool:
        return content_type == "application/pdf"


class DocxTextExtractor(TextExtractor):
    def extract(self, source: BinaryIO) -> str:
        from docx import Document
        from docx.opc.exceptions import PackageNotFoundError

        try:
            document = Document(source)
        except PackageNotFoundError as exc:
            raise ExtractionError("The file is not a readable .docx document.") from exc
        except Exception as exc:
            raise ExtractionError(f"DOCX extraction failed: {type(exc).__name__}") from exc

        parts = [p.text for p in document.paragraphs]
        # Tables are often the only content in a spec sheet or a report.
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts).strip()
        if not text:
            raise ExtractionError("The .docx document contains no text.")
        return text

    def handles(self, content_type: str) -> bool:
        return content_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )


#: Ordered most specific first. The allowlist in settings decides what can be
#: uploaded at all; this decides how it is read.
EXTRACTORS: tuple[TextExtractor, ...] = (
    PdfTextExtractor(),
    DocxTextExtractor(),
    HtmlTextExtractor(),
    PlainTextExtractor(),
)

SUPPORTED_CONTENT_TYPES = frozenset(CONTENT_TYPE_EXTENSIONS)


def extractor_for(content_type: str) -> TextExtractor:
    for extractor in EXTRACTORS:
        if extractor.handles(content_type):
            return extractor
    raise ExtractionError(f"No extractor for content type '{content_type}'.")


def extract_from_path(content_type: str, path: Path) -> str:
    with path.open("rb") as handle:
        return extractor_for(content_type).extract(handle)


_WHITESPACE_RUN = re.compile(r"[^\S\n]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")
# Control characters excluding tab and newline, which are meaningful here.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize(text: str) -> str:
    """Make raw extracted text safe to embed and to compare.

    Extracted text is untrusted and messy: null bytes from a binary misread,
    control characters from a PDF, ragged indentation, and hard line breaks from
    wrapped prose. All of it ends up in an embedding and in an LLM prompt, so it
    is cleaned once, here, rather than defended against downstream.
    """
    if not text:
        return ""
    # NFKC folds ligatures and full-width forms, which otherwise fragment words.
    import unicodedata

    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = cleaned.replace(" ", " ").replace("", "")
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    cleaned = _BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()
