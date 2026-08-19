"""Deterministic Markdown/text parser adapter.

The parser never executes macros, scripts, formulas, or archives.  It
produces a verbatim NormalizedDocument whose text spans are owned by the
structure-v1 chunker; extracted links are treated as data.
"""

from __future__ import annotations

import re
from typing import Any

from plastic_promise.knowledge.contracts import NormalizedDocument

_H1_RE = re.compile(r"^ {0,3}#\s+(.+?)\s*$")
_NUL_SNIFF_BYTES = 8192


class MarkdownTextParserError(ValueError):
    """Raised for undecodable, binary, or oversized source material."""


class MarkdownTextParser:
    """Parse Markdown or plain text into a NormalizedDocument."""

    parser_id = "markdown-text-v1"
    parse_schema = "structure-v1"

    def __init__(self, *, max_bytes: int | None = None) -> None:
        self._max_bytes = max_bytes

    def parse(self, content: bytes) -> NormalizedDocument:
        if not isinstance(content, bytes):
            raise MarkdownTextParserError("source content must be bytes")
        if self._max_bytes is not None and len(content) > self._max_bytes:
            raise MarkdownTextParserError(
                f"source exceeds size limit ({len(content)} > {self._max_bytes} bytes)"
            )
        if b"\x00" in content[:_NUL_SNIFF_BYTES]:
            raise MarkdownTextParserError("binary content is not supported")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MarkdownTextParserError("source is not valid UTF-8 text") from exc
        if "\x00" in text:
            raise MarkdownTextParserError("binary content is not supported")
        title = self._extract_title(text)
        return NormalizedDocument(
            title=title,
            text=text,
            parser_id=self.parser_id,
            parse_schema=self.parse_schema,
        )

    @staticmethod
    def _extract_title(text: str) -> str:
        for line in text.splitlines():
            match = _H1_RE.match(line)
            if match:
                return match.group(1).strip()[:200]
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:200]
        return ""

    def extract_media_anchors(self, text: str) -> tuple[dict[str, Any], ...]:
        """Collect markdown image/link anchors as inert data (future use)."""
        anchors: list[dict[str, Any]] = []
        for pattern, kind in (
            (r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)", "image"),
            (r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)", "link"),
        ):
            for match in re.finditer(pattern, text):
                anchors.append(
                    {
                        "kind": kind,
                        "label": match.group(1),
                        "target": match.group(2),
                        "title": match.group(3) or None,
                        "offset": match.start(),
                    }
                )
        return tuple(anchors)
