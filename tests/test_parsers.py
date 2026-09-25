import base64
import zipfile

import pytest
from ebooklib import epub

from parsers import (
    ParsedBook,
    _clean_html,
    _split_html,
    parse_book,
    parse_epub,
    parse_fb2,
)

NS_FB21 = "http://www.gribuser.ru/xml/fb2.1"
NS_FB20 = "http://www.gribuser.ru/xml/fictionbook/2.0"

# 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------- helpers

def fb2_doc(body: str, *, ns: str = NS_FB21, title="Тестовая книга",
            binaries="") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<FictionBook xmlns="{ns}" xmlns:l="http://www.w3.org/1999/xlink">\n'
        "<description><title-info>"
        f"<book-title>{title}</book-title>"
        "<author><first-name>Иван</first-name><last-name>Петров</last-name></author>"
        "<genre>prose</genre>"
        "</title-info></description>\n"
        f"<body>{body}</body>\n"
        f"{binaries}"
        "</FictionBook>"
    )


def write_fb2(path, doc: str, encoding="utf-8"):
    path.write_bytes(doc.encode(encoding))
    return str(path)


def make_epub(path, chapters, *, cover=False, image=False):
    """chapters: [(title, html_body)]"""
    book = epub.EpubBook()
    book.set_identifier("test-id")
    book.set_title("Test Book")
    book.set_language("ru")
    book.add_author("Test Author")
    if cover:
        book.set_cover("cover.png", PNG)
    if image:
        img = epub.EpubImage(uid="pic", file_name="images/pic.png",
                             media_type="image/png", content=PNG)
        book.add_item(img)
    items = []
    for i, (title, body) in enumerate(chapters, 1):
        c = epub.EpubHtml(title=title, file_name=f"ch{i}.xhtml", lang="ru")
        c.content = (f"<h1>{title}</h1>{body}" if title else body)
        book.add_item(c)
        items.append(c)
    book.toc = tuple(items)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    spine = ["cover"] if cover else []
    spine += ["nav"] + items
    book.spine = spine
    epub.write_epub(str(path), book)
    return str(path)


# ---------------------------------------------------------------- FB2

@pytest.mark.parametrize("ns", [NS_FB21, NS_FB20])
def test_fb2_both_namespaces_parse(tmp_path, ns):
    """Регресс: оба неймспейса FB2 дают главы (раньше fictionbook/2.0 -> 0 глав)."""
    doc = fb2_doc(
        "<section><title><p>Первая</p></title>"
        "<p>Текст первой главы.</p></section>"
        "<section><title><p>Вторая</p></title>"
        "<p>Текст второй главы.</p></section>",
        ns=ns,
    )
    path = write_fb2(tmp_path / "b.fb2", doc)
    res = parse_fb2(path)
    assert res.title == "Тестовая книга"
    assert res.author == "Иван Петров"
    assert [t for t, _ in res.chapters] == ["Первая", "Вторая"]
    assert "Текст первой главы" in res.chapters[0][1]


def test_fb2_cover_from_binary(tmp_path):
    b64 = base64.b64encode(PNG).decode()
    doc = fb2_doc("<section><p>текст</p></section>")
    doc = doc.replace("</title-info>",
                      '<coverpage><image l:href="#cover.png"/></coverpage></title-info>')
    doc = doc.replace("</FictionBook>",
                      f'<binary id="cover.png" content-type="image/png">{b64}</binary>'
                      "</FictionBook>")
    path = write_fb2(tmp_path / "c.fb2", doc)
    res = parse_fb2(path)
    assert res.cover == PNG


def test_fb2_image_asset_in_chapter(tmp_path):
    b64 = base64.b64encode(PNG).decode()
    doc = fb2_doc(
        "<section><title><p>Г</p></title><p>до</p>"
        '<image l:href="#pic1"/><p>после</p></section>',
        binaries=f'<binary id="pic1" content-type="image/png">{b64}</binary>',
    )
    path = write_fb2(tmp_path / "i.fb2", doc)
    res = parse_fb2(path)
    assert len(res.assets) == 1
    assert "asset:" in res.chapters[0][1]
    assert PNG in [v[1] for v in res.assets.values()]


def test_fb2_notes_body_excluded(tmp_path):
    doc = fb2_doc(
        "<section><title><p>Глава</p></title><p>основной текст</p></section>"
    )
    doc = doc.replace("</FictionBook>",
                      '<body name="notes"><section><p>сноска лишняя</p></section></body>'
                      "</FictionBook>")
    path = write_fb2(tmp_path / "n.fb2", doc)
    res = parse_fb2(path)
    all_html = "".join(h for _, h in res.chapters)
    assert "основной текст" in all_html
    assert "сноска лишняя" not in all_html


def test_fb2_cp1251_decoded(tmp_path):
    doc = fb2_doc(
        "<section><title><p>Привет</p></title><p>текст с буквой я</p></section>"
    )
    path = write_fb2(tmp_path / "cp.fb2", doc, encoding="windows-1251")
    res = parse_fb2(path)
    assert res.chapters
    assert "буквой я" in res.chapters[0][1]


def test_fb2_nested_small_sections_one_chapter(tmp_path):
    """Мелкие вложенные секции остаются внутри одной главы."""
    inner = "".join(
        f'<section><title><p>Под{i}</p></title><p>{"x" * 60}</p></section>'
        for i in range(3)
    )
    doc = fb2_doc(f"<section><title><p>Раздел</p></title>{inner}</section>")
    path = write_fb2(tmp_path / "s.fb2", doc)
    res = parse_fb2(path)
    assert len(res.chapters) == 1
    title, html = res.chapters[0]
    assert title == "Раздел"
    assert "<h3>Под1</h3>" in html


def test_fb2_huge_section_split_with_prefix(tmp_path):
    """Секция больше CHAPTER_LIMIT (80k) делится, обёртка попадает в заголовки."""
    big = "".join(
        f'<section><title><p>Часть {i}</p></title>'
        f'<p>{"длинный текст " * 2600}</p></section>'
        for i in range(1, 4)
    )  # ~3 * 36000 > 80000
    doc = fb2_doc(f"<section><title><p>Обёртка</p></title>{big}</section>")
    path = write_fb2(tmp_path / "big.fb2", doc)
    res = parse_fb2(path)
    assert len(res.chapters) == 3
    for title, _ in res.chapters:
        assert title.startswith("Обёртка — Часть ")


def test_fb2_small_wrapper_glued_to_first_child(tmp_path):
    """Прямое содержимое обёртки <500 знаков (эпиграф) приклеивается, не создаёт главу."""
    big = "".join(
        f'<section><title><p>Глава {i}</p></title>'
        f'<p>{"содержание " * 3200}</p></section>'
        for i in range(1, 4)
    )
    doc = fb2_doc(
        "<section><title><p>Книга</p></title>"
        "<epigraph><p>Краткая мысль ума.</p></epigraph>"
        f"{big}</section>"
    )
    path = write_fb2(tmp_path / "ep.fb2", doc)
    res = parse_fb2(path)
    assert len(res.chapters) == 3
    assert "Краткая мысль ума" in res.chapters[0][1]
    assert "<blockquote>" in res.chapters[0][1]


def test_fb2_single_section_split(tmp_path):
    """Одна секция без подсекций, но большая — режется по абзацам."""
    paras = "".join(f"<p>{'раз ' * 400}</p>" for _ in range(30))
    doc = fb2_doc(f"<section>{paras}</section>")
    path = write_fb2(tmp_path / "one.fb2", doc)
    res = parse_fb2(path)
    assert len(res.chapters) > 1
    assert [t for t, _ in res.chapters] == [f"Глава {i}" for i in range(1, len(res.chapters) + 1)]


def test_fb2_inline_markup_preserved(tmp_path):
    doc = fb2_doc(
        "<section><p>до <strong>жирный</strong> и <emphasis>курсив</emphasis> "
        "<sup>верх</sup> после</p></section>"
    )
    path = write_fb2(tmp_path / "m.fb2", doc)
    res = parse_fb2(path)
    html = res.chapters[0][1]
    assert "<strong>жирный</strong>" in html
    assert "<em>курсив</em>" in html
    assert "<sup>верх</sup>" in html


def test_fb2_zip(tmp_path):
    doc = fb2_doc("<section><p>в архиве</p></section>")
    zpath = tmp_path / "book.fb2.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("book.fb2", doc)
    res = parse_fb2(str(zpath))
    assert "в архиве" in res.chapters[0][1]


# ---------------------------------------------------------------- EPUB

def test_epub_basic(tmp_path):
    path = make_epub(tmp_path / "b.epub", [
        ("Глава первая", "<p>Первый абзац.</p>"),
        ("Глава вторая", "<p>Второй абзац.</p>"),
    ])
    res = parse_epub(path)
    assert res.title == "Test Book"
    assert res.author == "Test Author"
    assert [t for t, _ in res.chapters] == ["Глава первая", "Глава вторая"]
    assert "Первый абзац" in res.chapters[0][1]


def test_epub_cover(tmp_path):
    path = make_epub(tmp_path / "c.epub", [("Т", "<p>текст</p>")], cover=True)
    res = parse_epub(path)
    assert res.cover == PNG


def test_epub_image_asset(tmp_path):
    path = make_epub(
        tmp_path / "i.epub",
        [("Т", '<p><img src="images/pic.png" alt=""/></p><p>текст</p>')],
        image=True,
    )
    res = parse_epub(path)
    assert len(res.assets) == 1
    assert "asset:" in res.chapters[0][1]


def test_epub_cover_page_skipped(tmp_path):
    """Страница только с обложкой не превращается в главу-пустышку."""
    path = make_epub(tmp_path / "cp.epub", [
        ("", '<p><img src="images/pic.png" alt="cover"/></p>'),
        ("Текст", "<p>живой текст</p>"),
    ], image=True)
    res = parse_epub(path)
    titles = [t for t, _ in res.chapters]
    assert len(res.chapters) == 1
    assert titles == ["Текст"]
    assert "живой текст" in res.chapters[0][1]


def test_epub_single_doc_split(tmp_path):
    paras = "".join(f"<p>{'слово ' * 300}</p>" for _ in range(15))
    path = make_epub(tmp_path / "one.epub", [("Т", paras)])
    res = parse_epub(path)
    assert len(res.chapters) > 1
    assert all(t.startswith("Глава ") for t, _ in res.chapters)


def test_parse_book_dispatch(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hi")
    with pytest.raises(ValueError):
        parse_book(str(p))


# ---------------------------------------------------------------- PDF

def _make_pdf(path, pages):
    """pages: [(heading, [paragraph, ...])] — heading рисуется крупным bold."""
    import pymupdf

    doc = pymupdf.open()
    for heading, paras in pages:
        page = doc.new_page()
        y = 72
        if heading:
            page.insert_text((72, y), heading, fontsize=20, fontname="hebo")
            y += 40
        for p in paras:
            page.insert_text((72, y), p, fontsize=11, fontname="helv")
            y += 16
    doc.set_metadata({"title": "PDF книга", "author": "Автор ПДФ"})
    doc.save(str(path))
    doc.close()


def test_pdf_split_by_headings(tmp_path):
    from parsers import parse_pdf

    # Base14-шрифты pymupdf не содержат кириллицы — текст фикстуры латиницей
    path = tmp_path / "t.pdf"
    _make_pdf(path, [
        ("Chapter One", ["Paragraph one " * 8, "Paragraph two " * 8]),
        ("Chapter Two", ["Text of second paragraph " * 8]),
    ])
    res = parse_pdf(path)
    assert res.title == "PDF книга"
    assert res.author == "Автор ПДФ"
    assert len(res.chapters) == 2
    assert res.chapters[0][0] == "Chapter One"
    assert "Paragraph one" in res.chapters[0][1]
    assert res.cover and res.cover[:3] in (b"\xff\xd8\xff", b"\x89PN")


def test_pdf_fallback_split(tmp_path):
    from parsers import parse_pdf

    path = tmp_path / "plain.pdf"
    # без заголовков: обычный кегль → нарезка по абзацам
    # (строки короткие — длинные insert_text обрезает по ширине страницы)
    paras = ["Plain text paragraph " * 5 for _ in range(60)]
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for text in paras:
        if y > 800:
            page = doc.new_page()
            y = 72
        page.insert_text((72, y), text, fontsize=11, fontname="helv")
        y += 18
    doc.save(str(path))
    doc.close()
    res = parse_pdf(path)
    assert len(res.chapters) >= 2
    assert all(t.startswith("Глава ") for t, _ in res.chapters)


# ---------------------------------------------------------------- sanitizer

def test_clean_drops_script_style():
    out = _clean_html('<p>ok</p><script>evil()</script><style>p{}</style>')
    assert "evil" not in out
    assert "script" not in out
    assert "ok" in out


def test_clean_nested_script_removed():
    """Регресс: вложенный DROP внутри DROP (снимок итерации)."""
    out = _clean_html('<div><script>var x="<script>b</script>";</script>text</div>')
    assert "script" not in out
    assert "text" in out


def test_clean_unwraps_unknown_tags():
    out = _clean_html('<font color="red">текст</font>')
    assert "<font" not in out
    assert "текст" in out


def test_clean_strips_event_handlers():
    out = _clean_html('<p onclick="hack()">клик</p>')
    assert "onclick" not in out
    assert "клик" in out


def test_clean_keeps_img_attrs():
    out = _clean_html('<img src="a.png" alt="x" onerror="hack()">')
    assert 'src="a.png"' in out
    assert "onerror" not in out


def test_clean_strips_style_attr():
    out = _clean_html('<p style="color:red">t</p>')
    assert "style" not in out
    assert ">t<" in out


def test_split_html_chunks():
    body = "".join(f"<p>{'буква ' * 300}</p>" for _ in range(10))
    parts = _split_html(body, "Глава")
    assert len(parts) > 1
    assert parts[0][0] == "Глава 1"
    joined = "".join(h for _, h in parts)
    assert "буква" in joined


def test_parsed_book_defaults():
    b = ParsedBook()
    assert b.chapters == [] and b.assets == {} and b.cover is None
    assert b.sources == []


def test_clean_html_keeps_anchor_ids():
    """Якоря целей сносок (id) переживают санитизацию — без них /goto не finds."""
    out = _clean_html(
        '<section id="vv-1"><p id="p1">Текст</p>'
        '<a id="a1">метка</a><a href="x" title="t" id="a2">y</a></section>'
    )
    assert 'id="vv-1"' in out
    assert 'id="p1"' in out
    assert 'id="a1"' in out
    assert 'id="a2"' in out and 'href="x"' in out
    assert "<section" not in out  # неизвестный тег раскрыт, якорь сохранён


def test_epub_sources_align_with_chapters(tmp_path):
    """EPUB: у каждой главы есть файл-источник для /goto-ссылок."""
    path = make_epub(
        tmp_path / "b.epub",
        [
            ("Первая", '<p>один <a href="ch2.xhtml#to-second">к второй</a></p>'),
            ("Вторая", '<p><span id="to-second">два</span></p>'),
        ],
    )
    res = parse_epub(path)
    assert len(res.chapters) == len(res.sources) == 2
    assert res.sources == ["ch1.xhtml", "ch2.xhtml"]
    # href на месте (переписание в /goto — забота app._store_book)
    assert 'href="ch2.xhtml#to-second"' in res.chapters[0][1]
    # якорь во второй главе сохранён
    assert 'id="to-second"' in res.chapters[1][1]


# ---------------------------------------------------------------- security: схемы URL

def test_clean_strips_javascript_href():
    out = _clean_html('<p><a href="javascript:alert(1)">клик</a></p>')
    assert "javascript:" not in out
    assert "клик" in out


def test_clean_strips_data_and_obfuscated_schemes():
    out = _clean_html('<a href="data:text/html;base64,PHN2Zz4=">x</a>')
    assert "data:" not in out
    out = _clean_html('<a href="java\tscript:alert(1)">y</a>')
    assert "script:alert" not in out
    out = _clean_html('<img src="javascript:alert(1)" alt="i">')
    assert "javascript:" not in out


def test_clean_keeps_safe_urls():
    out = _clean_html(
        '<a href="https://example.com/a?b=1">a</a>'
        '<a href="../x.xhtml#n1">b</a>'
        '<a href="#anchor">c</a>'
        '<img src="/media/images/1/x.png" alt="i">'
        '<a href="mailto:author@example.com">m</a>'
    )
    assert 'href="https://example.com/a?b=1"' in out
    assert 'href="../x.xhtml#n1"' in out
    assert 'href="#anchor"' in out
    assert 'src="/media/images/1/x.png"' in out
    assert 'href="mailto:author@example.com"' in out
