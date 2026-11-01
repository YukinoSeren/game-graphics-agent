"""Public HTML reader: preserve headings, table coordinates and image leads."""

import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .schema import Block, Document


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("only public HTTP(S) URLs without credentials are supported")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(query), ""))


def _table_rows(table):
    grid = {}
    rows = table.find_all("tr")
    for r, tr in enumerate(rows):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in grid:
                c += 1
            try:
                rowspan = min(100, max(1, int(cell.get("rowspan", 1))))
                colspan = min(100, max(1, int(cell.get("colspan", 1))))
            except ValueError:
                rowspan = colspan = 1
            text = cell.get_text(" ", strip=True)
            for rr in range(r, min(len(rows), r + rowspan)):
                for cc in range(c, c + colspan):
                    grid[rr, cc] = text
            c += colspan
    width = max((c for r, c in grid), default=-1) + 1
    return [[grid.get((r, c), "") for c in range(width)] for r in range(len(rows))]


def parse_html(url: str, html: str, max_chars: int = 100_000) -> Document:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "nav", "footer", "noscript", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks, images, section = [], [], []
    size, truncated = 0, False
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "img"]):
        if node.find_parent("table") or (node.name == "p" and node.find_parent("li")):
            continue
        if node.name == "img":
            src = node.get("src") or node.get("data-src")
            if src:
                images.append({"url": urljoin(url, src), "alt": node.get("alt", ""), "section": section.copy(), "status": "not_read"})
            continue
        rows = _table_rows(node) if node.name == "table" else []
        text = "\n".join(" | ".join(row) for row in rows) if rows else node.get_text(" ", strip=True)
        if not text:
            continue
        if size + len(text) > max_chars:
            truncated = True
            break  # Never cut a table halfway and claim complete capture.
        size += len(text)
        kind = "table" if node.name == "table" else "paragraph"
        if node.name.startswith("h"):
            level = int(node.name[1])
            section = section[:level - 1] + [text]
            kind = "heading"
        blocks.append(Block(id=f"b{len(blocks)}", kind=kind, section=section.copy(), text=text, rows=rows))
    stable = json.dumps([b.model_dump() for b in blocks], ensure_ascii=False, sort_keys=True)
    return Document(url=canonical_url(url), title=title, content_hash=hashlib.sha256(stable.encode()).hexdigest(), blocks=blocks, images=images, truncated=truncated)
