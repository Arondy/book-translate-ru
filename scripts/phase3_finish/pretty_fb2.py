from __future__ import annotations

import argparse
from pathlib import Path

from lxml import etree

BLOCK_CONTAINERS = {
    "FictionBook",
    "description",
    "title-info",
    "src-title-info",
    "document-info",
    "publish-info",
    "custom-info",
    "body",
    "section",
    "title",
    "subtitle",
    "annotation",
    "epigraph",
    "cite",
    "poem",
    "stanza",
    "table",
    "tr",
    "th",
    "td",
}

SINGLE_LINE_CONTAINERS = {
    "p",
    "v",
    "text-author",
}

VOID_TAGS = {
    "image",
    "empty-line",
}

INLINE_TAGS = {
    "a",
    "emphasis",
    "strong",
    "style",
    "strikethrough",
    "sub",
    "sup",
    "code",
}


def local_name(tag) -> str:
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return etree.QName(tag).localname
    return tag


def has_sig_text(text: str | None) -> bool:
    return bool(text and text.strip())


def should_format_as_multiline(el: etree._Element) -> bool:
    name = local_name(el.tag)
    if name in BLOCK_CONTAINERS:
        return True

    if name == "p":
        if len(el) == 1 and not has_sig_text(el.text):
            child_name = local_name(el[0].tag)
            if child_name in VOID_TAGS or child_name in BLOCK_CONTAINERS:
                return True
        return False

    return False


def format_tree(el: etree._Element, level: int = 0) -> None:
    if not isinstance(el.tag, str):
        return

    # First recurse into element children.
    for child in el:
        format_tree(child, level + 1)

    name = local_name(el.tag)
    multiline = should_format_as_multiline(el)

    if not multiline:
        return

    indent = "  " * level
    child_indent = "  " * (level + 1)

    if len(el):
        # Opening indentation for first child.
        if not has_sig_text(el.text):
            el.text = "\n" + child_indent

        # Child tails.
        for i, child in enumerate(el):
            if not isinstance(child.tag, str):
                continue

            child_name = local_name(child.tag)
            is_last = i == len(el) - 1

            if is_last:
                if not has_sig_text(child.tail):
                    child.tail = "\n" + indent
            else:
                if not has_sig_text(child.tail):
                    # Small readability improvement between top-level sections.
                    if name == "body" and child_name == "section" and local_name(el[i + 1].tag) == "section":
                        child.tail = "\n\n" + child_indent
                    else:
                        child.tail = "\n" + child_indent
    else:
        # Leaf block containers still need tail indentation.
        if level > 0 and not has_sig_text(el.tail):
            el.tail = "\n" + indent


def _first_visible_char(el: etree._Element) -> str:
    if has_sig_text(el.text):
        return el.text.lstrip()[0]
    for child in el:
        if isinstance(child.tag, str):
            c = _first_visible_char(child)
            if c:
                return c
    return ""


def _last_visible_char(el: etree._Element) -> str:
    if len(el):
        last = el[-1]
        if isinstance(last.tag, str):
            c = _last_visible_char(last)
            if c:
                return c
            if has_sig_text(last.tail):
                return last.tail.rstrip()[-1]
    if has_sig_text(el.text):
        return el.text.rstrip()[-1]
    return ""


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def normalize_emphasis_spaces(root: etree._Element) -> None:
    """Insert a space around <emphasis> only between two word characters.

    A space is needed when inline markup glues two words (`слово*курсив*`).
    It must NOT be added next to punctuation: `*слово*, а?` and `*слово*.`
    would otherwise become "слово , а?" / "слово .".
    """
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue

        for child in el:
            if not isinstance(child.tag, str):
                continue

            if local_name(child.tag) != "emphasis":
                continue

            prev = child.getprevious()
            nxt = child.getnext()

            # Слева: только если emphasis не первый тег и между текстом
            # родителя и emphasis встретятся два словарных символа.
            if prev is None and has_sig_text(el.text) and has_sig_text(child.text):
                if _is_word_char(el.text.rstrip()[-1]) and _is_word_char(child.text[0]):
                    if not el.text[-1].isspace():
                        el.text += " "

            # Справа: только если emphasis последний тег и его tail начинается
            # со словарного символа, а сам emphasis кончается словарным.
            if nxt is None and has_sig_text(child.tail):
                if _is_word_char(_last_visible_char(child)) and _is_word_char(child.tail.lstrip()[0]):
                    if not child.tail[0].isspace():
                        child.tail = " " + child.tail


def format_fb2(input_path: Path, output_path: Path) -> None:
    parser = etree.XMLParser(
        remove_blank_text=True,
        strip_cdata=False,
        recover=False,
        huge_tree=True,
    )

    tree = etree.parse(str(input_path), parser)
    root = tree.getroot()

    format_tree(root, 0)
    normalize_emphasis_spaces(root)

    tree.write(
        str(output_path),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
        standalone=None,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Format FB2 XML without breaking inline text.")
    ap.add_argument("input", type=Path, help="Input .fb2 file")
    ap.add_argument("output", type=Path, help="Output .fb2 file")
    args = ap.parse_args()

    format_fb2(args.input, args.output)


if __name__ == "__main__":
    main()
