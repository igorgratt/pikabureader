import posixpath
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field

from ebooklib import ITEM_COVER, ITEM_IMAGE, ITEM_NAVIGATION, ITEM_STYLE, epub

ALLOWED = {
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "em", "strong", "i", "b",
    "u", "s", "blockquote", "ul", "ol", "li", "a", "img", "hr", "figure",
    "figcaption", "sup", "sub", "pre", "code", "table", "tbody", "thead",
    "tr", "td", "th", "span", "div",
}
DROP = {"script", "style", "head", "meta", "link", "title", "noscript"}

from lxml import etree, html as lhtml


@dataclass
class ParsedBook:
    title: str = ""
    author: str = ""
    tags: str = ""
    description: str = ""
    cover: bytes | None = None
    chapters: list = field(default_factory=list)  # [(title, html)]
    assets: dict = field(default_factory=dict)  # name -> bytes
    sources: list = field(default_factory=list)  # файл-источник главы (EPUB)


def _tag_of(el) -> str:
    if not isinstance(el.tag, str):
        return ""
    return el.tag.split("}")[-1].lower()


def _clean_fragment(raw: bytes | str) -> etree._Element:
    """Parse arbitrary HTML fragment, drop everything not in ALLOWED."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    raw = re.sub(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", "", raw, flags=re.I | re.S)
    try:
        root = lhtml.fromstring(f"<div>{raw}</div>")
    except Exception:
        root = lhtml.fromstring(f"<div>{lhtml.escape(raw)}</div>")
    if root.tag != "div":
        wrap = lhtml.Element("div")
        wrap.append(root)
        root = wrap

    # pass 1: drop forbidden subtrees (snapshot iteration, no live mutation)
    for el in list(root.iter()):
        if _tag_of(el) in DROP and el is not root:
            parent = el.getparent()
            if parent is not None:
                idx = list(parent).index(el)
                if el.tail:
                    if idx > 0:
                        prev = parent[idx - 1]
                        prev.tail = (prev.tail or "") + el.tail
                    else:
                        parent.text = (parent.text or "") + el.tail
                parent.remove(el)

    # pass 2: unwrap unknown tags, sanitize attributes
    for el in list(root.iter()):
        if el is root or not isinstance(el.tag, str):
            continue
        tag = _tag_of(el)
        if tag in ALLOWED:
            if el.tag != tag:
                el.tag = tag
            if tag == "img":
                for attr in list(el.attrib):
                    if attr.lower() not in ("src", "alt", "loading"):
                        del el.attrib[attr]
            elif tag == "a":
                for attr in list(el.attrib):
                    if attr.lower() not in ("href", "title", "id"):
                        del el.attrib[attr]
            else:
                keep = el.get("id")
                el.attrib.clear()
                if keep is not None:
                    el.set("id", keep)
        else:
            parent = el.getparent()
            if parent is None:
                continue
            if el.get("id"):
                # цель внутрикнижной ссылки (сноска) — сохраняем якорь,
                # unwrap бы его потерял
                el.tag = "span"
                for attr in list(el.attrib):
                    if attr != "id":
                        del el.attrib[attr]
                continue
            # unwrap: содержимое (text + дети + tail) переходит на место тега
            if el.text:
                span = lhtml.Element("span")
                span.text = el.text
                el.insert(0, span)
            idx = list(parent).index(el)
            children = list(el)
            for i, child in enumerate(children):
                el.remove(child)
                parent.insert(idx + i, child)
            if children:
                last = parent[idx + len(children) - 1]
                last.tail = (last.tail or "") + (el.tail or "")
            elif idx > 0:
                prev = parent[idx - 1]
                prev.tail = (prev.tail or "") + (el.tail or "")
            else:
                parent.text = (parent.text or "") + (el.tail or "")
            parent.remove(el)

    # unwrap the wrapper itself: hoist children out
    while len(root) == 1 and _tag_of(root[0]) == "div":
        only = root[0]
        root.remove(only)
        for child in reversed(list(only)):
            only.remove(child)
            root.insert(0, child)
        if only.text:
            root.text = (root.text or "") + only.text
    return root


def _clean_html(raw: bytes | str) -> str:
    root = _clean_fragment(raw)
    parts = [lhtml.tostring(child, encoding="unicode") for child in root]
    if not parts:
        parts = [lhtml.tostring(root, encoding="unicode")]
    out = "".join(parts)
    out = re.sub(r"<(p|h1|h2|h3|h4|h5|h6|blockquote|div|ul|ol|table|figure)\b[^>]*>\s*</\1>", "", out)
    return out.strip()


def _text_of(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip()


def parse_epub(path: str) -> ParsedBook:
    book = epub.read_epub(path, options={"ignore_ncx": False})
    res = ParsedBook()

    def dc(name, default=""):
        try:
            vals = book.get_metadata("DC", name)
            if vals:
                return str(vals[0][0])
        except Exception:
            pass
        return default

    res.title = dc("title") or "Без названия"
    res.author = dc("creator") or "Неизвестный автор"
    res.tags = dc("subject")
    res.description = dc("description")

    # cover
    for item in book.get_items():
        if item.get_type() in (ITEM_COVER, ITEM_IMAGE) and "cover" in (
            item.get_name() or ""
        ).lower():
            res.cover = item.get_content()
            break
    if not res.cover:
        for item in book.get_items_of_type(ITEM_COVER):
            res.cover = item.get_content()
            break

    # image map: resolved absolute path in archive -> item
    img_map: dict[str, epub.EpubItem] = {}
    for item in book.get_items_of_type(ITEM_IMAGE):
        img_map[posixpath.normpath(item.get_name())] = item

    def resolve(doc_path: str, href: str) -> str:
        href = urllib.parse.unquote(href.split("#")[0]).strip()
        if not href:
            return ""
        return posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), href))

    # titles from TOC
    toc_titles: dict[str, str] = {}

    def walk_toc(entries):
        for e in entries:
            if isinstance(e, (list, tuple)):
                walk_toc(e)
            elif isinstance(e, epub.Link):
                key = posixpath.normpath(urllib.parse.unquote(e.href.split("#")[0]))
                toc_titles.setdefault(key, e.title or "")

    walk_toc(book.toc)

    spine_docs = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() == ITEM_NAVIGATION:
            continue
        # EpubNav.get_type() -> ITEM_DOCUMENT, ловим по классу
        if isinstance(item, epub.EpubNav):
            continue
        if item.get_type() == ITEM_STYLE:
            continue
        if not item.get_name().lower().endswith((".xhtml", ".html", ".htm", ".xml")):
            continue
        spine_docs.append(item)

    pending_imgs: list[str] = []
    for idx, item in enumerate(spine_docs):
        raw = item.get_content()
        root = _clean_fragment(raw)

        # rewrite img src -> asset placeholder
        for img in root.iter("img"):
            src = img.get("src") or ""
            if not src or src.startswith(("http:", "https:", "data:")):
                if not src.startswith("data:"):
                    img.getparent().remove(img)
                continue
            resolved = resolve(item.get_name(), src)
            src_item = img_map.get(resolved)
            if src_item is None:
                # try url-decoded variant
                src_item = img_map.get(resolved.replace(" ", "_"))
            if src_item is None:
                img.getparent().remove(img)
                continue
            name = src_item.get_name()
            if name not in res.assets:
                short = f"img{len(res.assets)}_{posixpath.basename(name)}"
                res.assets[name] = (short, src_item.get_content())
            img.set("src", f"asset:{res.assets[name][0]}")
            img.set("loading", "lazy")

        # title: TOC name or first heading
        title = toc_titles.get(posixpath.normpath(item.get_name()))
        if not title:
            for h in ("h1", "h2", "h3"):
                found = root.find(f".//{h}")
                if found is not None and _text_of(found):
                    title = _text_of(found)
                    found.getparent().remove(found)
                    break
        title = title or f"Глава {idx + 1}"

        body = _clean_html(lhtml.tostring(root, encoding="unicode"))
        if not _text_of(root).strip():
            # страница без текста: обложку пропускаем, иллюстрации приклеиваем
            # к следующей текстовой главе
            srcs = [im.get("src", "") + im.get("alt", "") for im in root.iter("img")]
            if not srcs or any("cover" in s.lower() for s in srcs):
                continue
            pending_imgs.append(body)
            continue
        if pending_imgs:
            body = "".join(pending_imgs) + body
            pending_imgs.clear()
        res.chapters.append((title, body))
        res.sources.append(posixpath.normpath(item.get_name()))

    # картинки в конце книги — приклеиваем к последней главе
    if pending_imgs and res.chapters:
        t, h = res.chapters[-1]
        res.chapters[-1] = (t, h + "".join(pending_imgs))
        pending_imgs.clear()

    # EPUB без spine-разбивки (один файл) — режем на куски по абзацам,
    # но заголовок сохраняем, если нарезка не понадобилась
    if len(res.chapters) <= 1 and res.chapters:
        title, body = res.chapters[0]
        split = _split_html(body, "Глава")
        if len(split) > 1:
            res.chapters = split
            res.sources = [res.sources[0] if res.sources else ""] * len(split)
    return res


def _split_html(body: str, prefix: str, per: int = 3500) -> list:
    """Split one big HTML body into chunks of ~per characters of text."""
    root = lhtml.fromstring(f"<div>{body}</div>")
    chunks: list[list[etree._Element]] = []
    cur: list[etree._Element] = []
    size = 0
    for child in root:
        text = _text_of(child)
        if size and size + len(text) > per:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(child)
        size += len(text)
    if cur:
        chunks.append(cur)
    if len(chunks) <= 1:
        return [(f"{prefix} 1", body)]
    out = []
    for i, ch in enumerate(chunks, 1):
        frag = "".join(
            lhtml.tostring(el, encoding="unicode") for el in ch
        )
        out.append((f"{prefix} {i}", frag))
    return out


def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_pdf(path: str) -> ParsedBook:
    """PDF: главы по заголовкам (эвристика по размеру/жирности шрифта),
    иначе нарезка по ~4000 знаков. Обложка — рендер первой страницы."""
    import os
    import statistics

    import pymupdf

    res = ParsedBook()
    doc = pymupdf.open(path)
    try:
        meta = doc.metadata or {}
        res.title = (meta.get("title") or "").strip() or os.path.splitext(
            os.path.basename(path)
        )[0]
        res.author = (meta.get("author") or "").strip() or "Неизвестный автор"

        # обложка (F2): первая страница как JPEG
        if doc.page_count:
            try:
                pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4))
                res.cover = pix.tobytes("jpeg")
            except Exception:
                pass

        # блоки текста: (text, size, bold)
        blocks: list[tuple[str, float, bool]] = []
        for page in doc:
            data = page.get_text("dict")
            for blk in data.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                # каждая строка — отдельный блок (иначе абзацы
                # склеиваются в один текст и нарезка не находит границ)
                for line in blk.get("lines", []):
                    spans = line.get("spans", [])
                    t = "".join(s.get("text", "") for s in spans).strip()
                    if not t:
                        continue
                    size = max((s.get("size", 0.0) for s in spans), default=0.0)
                    bold = any(
                        (s.get("flags", 0) & 16)
                        or "bold" in s.get("font", "").lower()
                        for s in spans
                    )
                    blocks.append((t, size, bold))
    finally:
        doc.close()

    if not blocks:
        return res

    sizes_sorted = sorted(s for _, s, _ in blocks if s > 0)
    med = statistics.median(sizes_sorted) if sizes_sorted else 12.0

    def is_heading(text: str, size: float, bold: bool) -> bool:
        if len(text) > 90 or len(text.split()) > 14:
            return False
        if text.endswith((".", ",", ";", ":", "…", "»")):
            return False
        if size >= med * 1.12 and (bold or size >= med * 1.25):
            return True
        return False

    chapters: list = []
    cur_title: str | None = None
    cur_body: list[str] = []

    def flush():
        nonlocal cur_title, cur_body
        if cur_title is None and not cur_body:
            return
        body = "".join(f"<p>{_esc_text(p)}</p>" for p in cur_body)
        title = cur_title or f"Глава {len(chapters) + 1}"
        if re.sub(r"<[^>]+>", "", body).strip():
            chapters.append((title, body))
        cur_title, cur_body = None, []

    for text, size, bold in blocks:
        if is_heading(text, size, bold):
            flush()
            cur_title = text
        else:
            cur_body.append(text)
    flush()

    # слишком мало заголовков — режем по абзацам
    if len(chapters) < 2:
        full = "".join(
            f"<p>{_esc_text(t)}</p>" for t, s, b in blocks if not is_heading(t, s, b)
        )
        split = _split_html(full, "Глава", per=4000)
        if len(split) > 1:
            res.chapters = split
        elif chapters:
            res.chapters = chapters
        elif full:
            res.chapters = [("Глава 1", full)]
    else:
        res.chapters = chapters
    return res


def parse_fb2(path: str) -> ParsedBook:
    import xml.etree.ElementTree as ET

    res = ParsedBook()
    XLINK = "{http://www.w3.org/1999/xlink}href"

    raw = None
    assets: dict[str, bytes] = {}
    fb2name = ""

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            fb2name = next((n for n in names if n.lower().endswith(".fb2")), "")
            if not fb2name:
                raise ValueError("В архиве не найден файл .fb2")
            raw = zf.read(fb2name)
            for n in names:
                if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    assets[posixpath.normpath(n)] = zf.read(n)
    else:
        with open(path, "rb") as f:
            raw = f.read()
        # standalone images beside fb2 file
        import os

        folder = os.path.dirname(path)
        for n in os.listdir(folder):
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                assets[n] = open(os.path.join(folder, n), "rb").read()

    # FB2 иногда в кодировке windows-1251
    text = None
    for enc in ("utf-8", "windows-1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    root = ET.fromstring(text)
    # FB2 бывает с двумя неймспейсами: fb2.1 (старый) и fictionbook/2.0
    if root.tag.startswith("{"):
        NS = root.tag[: root.tag.index("}") + 1]
    else:
        NS = ""

    # картинки, вшитые в FB2 через <binary id="...">base64</binary>
    import base64

    for b in root.iter(f"{NS}binary"):
        bid = b.get("id") or ""
        b64 = "".join((b.text or "").split())
        if bid and b64:
            try:
                assets[bid] = base64.b64decode(b64)
            except Exception:
                pass

    ti = root.find(f".//{NS}description/{NS}title-info")
    if ti is not None:
        bt = ti.find(f"{NS}book-title")
        res.title = (bt.text or "").strip() if bt is not None else ""
        author_el = ti.find(f"{NS}author")
        if author_el is not None:
            parts = [
                (author_el.findtext(f"{NS}first-name") or ""),
                (author_el.findtext(f"{NS}middle-name") or ""),
                (author_el.findtext(f"{NS}last-name") or ""),
            ]
            res.author = " ".join(p for p in parts if p).strip()
            if not res.author:
                res.author = author_el.findtext(f"{NS}nickname") or "Неизвестный автор"
        genres = [
            (g.text or "").strip()
            for g in ti.findall(f"{NS}genre")
            if g.text
        ]
        res.tags = ", ".join(genres)
        an = ti.find(f"{NS}annotation")
        if an is not None:
            res.description = "".join(an.itertext()).strip()[:2000]
        cp = ti.find(f"{NS}coverpage")
        if cp is not None:
            img = cp.find(f"{NS}image")
            if img is not None:
                href = (img.get(XLINK) or img.get("href") or "").lstrip("#")
                href = urllib.parse.unquote(href)
                for key, val in assets.items():
                    if key.endswith(href.lstrip("./")) or posixpath.basename(key) == posixpath.basename(href):
                        res.cover = val
                        break
    res.title = res.title or "Без названия"
    res.author = res.author or "Неизвестный автор"

    def asset_placeholder(name: str) -> str | None:
        if name not in res.assets:
            short = f"img{len(res.assets)}_{posixpath.basename(name)}"
            res.assets[name] = (short, assets[name])
        return f"asset:{res.assets[name][0]}"

    def find_asset(href: str) -> str | None:
        href = urllib.parse.unquote(href).lstrip("#")
        for key in assets:
            if key.endswith(href.lstrip("./")) or posixpath.basename(key) == posixpath.basename(href):
                return key
        return None

    def render_section(sec) -> str:
        """Прямое содержимое секции (без вложенных секций)."""
        out: list[str] = []
        for child in sec:
            if child.tag == f"{NS}title" or child.tag == f"{NS}section":
                continue
            if child.tag == f"{NS}image":
                href = (child.get(XLINK) or child.get("href") or "").lstrip("#")
                key = find_asset(href)
                if key:
                    out.append(f'<p><img src="{asset_placeholder(key)}" alt=""></p>')
            elif child.tag == f"{NS}p":
                ptext = "".join(child.itertext())
                if ptext.strip():
                    out.append(f"<p>{_inline(child)}</p>")
            elif child.tag == f"{NS}subtitle":
                t = "".join(child.itertext()).strip()
                if t:
                    out.append(f"<p><b>{_esc(t)}</b></p>")
            elif child.tag in (f"{NS}cite", f"{NS}poem", f"{NS}text", f"{NS}epigraph"):
                ps = [p for p in child.iter(f"{NS}p") if "".join(p.itertext()).strip()]
                if ps:
                    for p in ps:
                        out.append(f"<blockquote><p>{_inline(p)}</p></blockquote>")
                else:
                    t = "".join(child.itertext()).strip()
                    if t:
                        out.append(f"<blockquote><p>{_esc(t)}</p></blockquote>")
            elif child.tag == f"{NS}table":
                out.append(f"<blockquote><p>{''.join(child.itertext())}</p></blockquote>")
        return "".join(out)

    def _inline(el) -> str:
        """Render element children preserving <strong>/<em>/<a>-like tags."""
        parts: list[str] = []
        if el.text:
            parts.append(_esc(el.text))
        for child in el:
            tag = child.tag.split("}")[-1].lower()
            inner = _inline(child)
            if tag == "strong":
                parts.append(f"<strong>{inner}</strong>")
            elif tag == "emphasis":
                parts.append(f"<em>{inner}</em>")
            elif tag in ("sup", "sub", "code"):
                parts.append(f"<{tag}>{inner}</{tag}>")
            elif tag in ("strikethrough", "del"):
                parts.append(f"<s>{inner}</s>")
            elif tag == "image":
                href = (child.get(XLINK) or child.get("href") or "").lstrip("#")
                key = find_asset(href)
                if key:
                    parts.append(f'<img src="{asset_placeholder(key)}" alt="">')
            else:
                parts.append(inner)
            if child.tail:
                parts.append(_esc(child.tail))
        return "".join(parts)

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # основной body: не notes/comments (сноски и комментарии не читаем)
    bodies = root.findall(f"{NS}body")
    body = None
    for b in bodies:
        if (b.get("name") or "").lower() not in ("notes", "comments", "footnotes", "footnote"):
            body = b
            break
    if body is None and bodies:
        body = bodies[0]
    sections = body.findall(f"{NS}section") if body is not None else []

    # лимит: секция больше этого размера делится на подсекции-главы
    CHAPTER_LIMIT = 80000
    # меньше этого — содержимое обёртки приклеивается к первой подсекции
    MIN_DIRECT = 500

    def section_title(sec) -> str:
        title_el = sec.find(f"{NS}title")
        if title_el is None:
            return ""
        return re.sub(r"\s+", " ", "".join(title_el.itertext())).strip()

    def text_len(html: str) -> int:
        return len(re.sub(r"<[^>]+>", "", html))

    def flatten(sec, depth: int = 1) -> str:
        """Прямое содержимое + все вложенные секции с заголовками-h3/h4."""
        out = [render_section(sec)]
        for inn in sec.findall(f"{NS}section"):
            it = section_title(inn)
            if it:
                h = "h3" if depth == 1 else "h4"
                out.append(f"<{h}>{_esc(it)}</{h}>")
            out.append(flatten(inn, depth + 1))
        return "".join(out)

    def collect(sec, hint: str) -> list:
        own = section_title(sec) or hint
        inners = sec.findall(f"{NS}section")
        if not inners:
            direct = render_section(sec)
            return [(own, direct)] if direct.strip() else []
        full = flatten(sec)
        if text_len(full) <= CHAPTER_LIMIT:
            return [(own, full)]
        # слишком большая — каждая подсекция отдельной главой,
        # в заголовки подсекций добавляем имя обёртки, чтобы не терять контекст
        out: list = []
        direct = render_section(sec)
        if text_len(direct) >= MIN_DIRECT:
            out.append((own, direct))
            direct, child_prefix = "", ""
        else:
            child_prefix = f"{own} — "
        for j, inn in enumerate(inners, 1):
            got = collect(inn, f"{own}.{j}")
            if direct.strip() and got:
                t0, h0 = got[0]
                got[0] = (t0, direct + h0)
                direct = ""
            out.extend((child_prefix + t, h) for t, h in got)
        return out or [(own, full)]

    for i, sec in enumerate(sections, 1):
        res.chapters.extend(collect(sec, f"Глава {i}"))

    # одна секция без разбивки — режем по абзацам (если нарезка понадобилась)
    if len(res.chapters) <= 1 and res.chapters:
        title, body_html = res.chapters[0]
        split = _split_html(body_html, "Глава")
        res.chapters = split if len(split) > 1 else [(title, body_html)]

    # подчистить итоговый html через ALLOWED-фильтр
    cleaned = []
    for title, html in res.chapters:
        cleaned.append((title, _clean_html(html)))
    res.chapters = cleaned
    return res


def parse_book(path: str) -> ParsedBook:
    lower = path.lower()
    if lower.endswith(".epub"):
        return parse_epub(path)
    if lower.endswith(".pdf"):
        return parse_pdf(path)
    if lower.endswith((".fb2", ".fb2.zip", ".zip")):
        return parse_fb2(path)
    raise ValueError(f"Неизвестный формат: {path}")
