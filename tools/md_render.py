#!/usr/bin/env python3
"""Small, escape-first Markdown to HTML renderer for the tracker board.

Covers the subset the workspace's own files use (form packs, referral DMs,
saved postings, interview prep): `#`-`###` headings, pipe tables, fenced code
blocks, `-` bullets (one nesting level, `- [ ]` checkboxes), numbered lists,
`**bold**`, `` `code` ``, `[text](url)` links, bare URLs and `---` rules.

Every text span is HTML-escaped before markup is applied, links are emitted
only for http(s) URLs, and no raw HTML passes through. Saved postings are
third-party text (SECURITY.md), so render them with autolink=False: no link
is created from their content at all.

Usage as a library:  from md_render import render; html = render(text)
Usage from a shell:  python tools/md_render.py FILE.md [--no-links] > out.html
"""

import html
import re
import sys

LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
RULE_RE = re.compile(r"^\s*-{3,}\s*$")
SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
TRAILING = ".,;:!?'\""


def _anchor(url, label):
    href = html.escape(url, quote=True)
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer nofollow">{label}</a>'


def _text(segment, autolink):
    """Escape a non-code span, then apply links and bold."""
    out, pos = [], 0
    for m in LINK_RE.finditer(segment):
        out.append(_plain(segment[pos:m.start()], autolink))
        label = BOLD_RE.sub(r"<strong>\1</strong>", html.escape(m.group(1)))
        out.append(_anchor(m.group(2), label) if autolink else f"{label} ({html.escape(m.group(2))})")
        pos = m.end()
    out.append(_plain(segment[pos:], autolink))
    return "".join(out)


def _plain(segment, autolink):
    out, pos = [], 0
    for m in URL_RE.finditer(segment) if autolink else ():
        url = m.group(0).rstrip(TRAILING)
        out.append(_bold(segment[pos:m.start()]))
        out.append(_anchor(url, html.escape(url)))
        pos = m.start() + len(url)
    out.append(_bold(segment[pos:]))
    return "".join(out)


def _bold(segment):
    return BOLD_RE.sub(r"<strong>\1</strong>", html.escape(segment))


def inline(text, autolink=True):
    """Render one line of inline Markdown to safe HTML."""
    parts = re.split(r"(`[^`\n]+`)", text)
    return "".join(f"<code>{html.escape(p[1:-1])}</code>" if p.startswith("`") and p.endswith("`") and len(p) > 1
                   else _text(p, autolink) for p in parts)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _table(rows, autolink):
    head, body = rows[0], rows[1:]
    copy_cols = {i for i, h in enumerate(head) if h.lower().startswith("paste or select")}
    out = ['<div class="table"><table><thead><tr>']
    out += [f"<th>{inline(h, autolink)}</th>" for h in head]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for i, cell in enumerate(row + [""] * (len(head) - len(row))):
            rendered = inline(cell, autolink)
            if i in copy_cols and cell and "FILL IN" not in cell and "YOUR CALL" not in cell:
                rendered = f'<span class="copytext">{rendered}</span><button type="button" class="copy" aria-label="Copy">Copy</button>'
            out.append(f"<td>{rendered}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render(text, autolink=True):
    lines = text.replace("\r\n", "\n").split("\n")
    out, para, i = [], [], 0

    def flush():
        if para:
            out.append("<p>" + "<br>".join(inline(p, autolink) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            flush()
            block, i = [], i + 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append('<div class="code"><button type="button" class="copy" aria-label="Copy">Copy</button>'
                       f"<pre><code>{html.escape(chr(10).join(block))}</code></pre></div>")
            continue
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2), autolink)}</h{level}>")
            i += 1
            continue
        if RULE_RE.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and SEP_RE.match(lines[i + 1]):
            flush()
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append(_table(rows, autolink))
            continue
        if BULLET_RE.match(line):
            flush()
            out.append("<ul>")
            nested = False
            while i < len(lines) and BULLET_RE.match(lines[i]):
                indent, item = BULLET_RE.match(lines[i]).groups()
                deep = len(indent.expandtabs(4)) >= 2
                if deep and not nested:
                    out.append("<ul>")
                elif not deep and nested:
                    out.append("</ul>")
                nested = deep
                box = re.match(r"^\[( |x|X)\]\s+(.*)$", item)
                if box:
                    checked = " checked" if box.group(1).lower() == "x" else ""
                    out.append(f'<li class="task"><input type="checkbox" disabled{checked}> {inline(box.group(2), autolink)}</li>')
                else:
                    out.append(f"<li>{inline(item, autolink)}</li>")
                i += 1
            out.append("</ul></ul>" if nested else "</ul>")
            continue
        if NUMBER_RE.match(line):
            flush()
            out.append("<ol>")
            while i < len(lines) and NUMBER_RE.match(lines[i]):
                out.append(f"<li>{inline(NUMBER_RE.match(lines[i]).group(1), autolink)}</li>")
                i += 1
            out.append("</ol>")
            continue
        if not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as f:
        sys.stdout.write(render(f.read(), autolink="--no-links" not in sys.argv))
