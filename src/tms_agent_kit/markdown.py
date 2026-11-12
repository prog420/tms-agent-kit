"""A small Markdown to HTML renderer for run notes and bug files in the report.

Supports headings, paragraphs, lists (nested by indentation), pipe tables, block quotes,
fenced code, code spans, bold, links, images and bare URLs. All text is escaped; raw HTML
is shown as text. `link` maps a link target to an href, or None to drop the link.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html import escape
from pathlib import PurePosixPath

Link = Callable[[str], str | None]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_INLINE_RE = re.compile(
    r"`([^`]+)`"  # code span
    r"|(!?)\[([^\]]*)\]\(\s*(?:<([^>]*)>|([^)\s]+))(?:\s+\"[^\"]*\")?\s*\)"  # link or image
    r"|(https?://[^\s<>()]+[^\s<>().,;:!?])"  # bare URL
)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def render_markdown(text: str, link: Link, heading_offset: int = 2) -> str:
    """HTML for a Markdown text. `heading_offset` shifts heading levels: `##` becomes h4."""
    return _Blocks(link, heading_offset).render(text.split("\n"))


def render_inline(text: str, link: Link) -> str:
    """HTML for one line or a plain-text block: code spans, bold, links; newlines are kept."""
    return _Blocks(link, 0).inline(text)


class _Blocks:
    def __init__(self, link: Link, offset: int) -> None:
        self.link = link
        self.offset = offset

    def render(self, lines: list[str]) -> str:
        out: list[str] = []
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                out.append(f"<p>{self.inline(' '.join(p.strip() for p in paragraph))}</p>")
                paragraph.clear()

        i = 0
        while i < len(lines):
            line = lines[i]
            fence = _FENCE_RE.match(line)
            heading = _HEADING_RE.match(line)
            if fence:
                flush()
                code: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith(fence.group(1)):
                    code.append(lines[i])
                    i += 1
                out.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")
            elif not line.strip():
                flush()
            elif heading:
                flush()
                level = min(len(heading.group(1)) + self.offset, 6)
                out.append(f"<h{level}>{self.inline(heading.group(2))}</h{level}>")
            elif line.lstrip().startswith("|") and i + 1 < len(lines) and _is_sep(lines[i + 1]):
                flush()
                rows = [line]
                i += 2
                while i < len(lines) and lines[i].lstrip().startswith("|"):
                    rows.append(lines[i])
                    i += 1
                out.append(self.table(rows))
                continue
            elif _ITEM_RE.match(line):
                flush()
                end = self.list_end(lines, i)
                out.append(self.bullets(lines[i:end]))
                i = end
                continue
            elif line.lstrip().startswith(">"):
                flush()
                quoted = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                out.append(f"<blockquote>{self.render(quoted)}</blockquote>")
                continue
            else:
                paragraph.append(line)
            i += 1
        flush()
        return "".join(out)

    def list_end(self, lines: list[str], start: int) -> int:
        """End of a list: a blank line not followed by an item or indented text, or other text."""
        indent = _indent(lines[start])
        ordered = _ordered(lines[start])
        i = start + 1
        while i < len(lines):
            line = lines[i]
            if _ITEM_RE.match(line) and _indent(line) <= indent and _ordered(line) != ordered:
                return i  # another kind of list starts
            if not line.strip():
                following = next((x for x in lines[i + 1 :] if x.strip()), "")
                if not following or (
                    _indent(following) <= indent and not _ITEM_RE.match(following)
                ):
                    return i
            elif _indent(line) <= indent and not _ITEM_RE.match(line):
                return i
            i += 1
        return i

    def bullets(self, lines: list[str]) -> str:
        indent = _indent(lines[0])
        ordered = _ordered(lines[0])
        items: list[list[str]] = []
        for line in lines:
            item = _ITEM_RE.match(line)
            if item and _indent(line) <= indent:
                items.append([item.group(3)])
            elif items:
                items[-1].append(line)
        html = []
        for item_lines in items:
            text = [item_lines[0]]
            rest = item_lines[1:]
            while rest and rest[0].strip() and not _ITEM_RE.match(rest[0]):
                text.append(rest.pop(0))
            body = self.inline(" ".join(t.strip() for t in text))
            nested = _dedent(rest)
            if any(x.strip() for x in nested):
                body += self.render(nested)
            html.append(f"<li>{body}</li>")
        tag = "ol" if ordered else "ul"
        return f"<{tag}>{''.join(html)}</{tag}>"

    def table(self, rows: list[str]) -> str:
        def cells(row: str) -> list[str]:
            row = row.strip().removeprefix("|")
            row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
            return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]

        head = "".join(f"<th>{self.inline(c)}</th>" for c in cells(rows[0]))
        body = "".join(
            "<tr>" + "".join(f"<td>{self.inline(c)}</td>" for c in cells(row)) + "</tr>"
            for row in rows[1:]
        )
        return (
            f"<div class=scroll><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>"
        )

    def inline(self, text: str) -> str:
        out: list[str] = []
        position = 0
        for match in _INLINE_RE.finditer(text):
            out.append(_bold(escape(text[position : match.start()])))
            position = match.end()
            code, image, label, angled, plain, url = match.groups()
            if code is not None:
                out.append(f"<code>{escape(code)}</code>")
                continue
            if url is not None:
                out.append(_anchor(url, escape(url)))
                continue
            target = (angled if angled is not None else plain or "").strip()
            href = self.link(target)
            if href is None:
                out.append(_bold(escape(label)))
            elif image and _local_image(href):
                src = escape(href, quote=True)
                alt = escape(label, quote=True)
                out.append(f'<a href="{src}" target="_blank"><img src="{src}" alt="{alt}"></a>')
            else:
                out.append(_anchor(href, _bold(escape(label or target))))
        out.append(_bold(escape(text[position:])))
        return "".join(out)


def _local_image(href: str) -> bool:
    """Only local images are shown: a report opened from disk loads nothing from the web."""
    local = not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href)
    return local and PurePosixPath(href.split("#")[0]).suffix.lower() in IMAGE_SUFFIXES


def _anchor(href: str, label: str) -> str:
    external = ' target="_blank" rel="noopener"' if re.match(r"^https?:", href) else ""
    return f'<a href="{escape(href, quote=True)}"{external}>{label}</a>'


def _bold(html: str) -> str:
    return _BOLD_RE.sub(r"<b>\1</b>", html)


def _ordered(line: str) -> bool:
    item = _ITEM_RE.match(line)
    return bool(item and item.group(2)[0].isdigit())


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _dedent(lines: list[str]) -> list[str]:
    widths = [_indent(x) for x in lines if x.strip()]
    cut = min(widths) if widths else 0
    return [x[cut:] for x in lines]


def _is_sep(line: str) -> bool:
    return "-" in line and bool(_TABLE_SEP_RE.match(line))
