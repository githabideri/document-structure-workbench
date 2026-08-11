"""Plain-text helpers shared by view fragments and page-feature rendering."""
from html.parser import HTMLParser


class _PageTextToPlain(HTMLParser):
    """Convert raw (possibly HTML-laced) page text into readable plain text.

    Visual-OCR models sometimes return the page transcription as HTML (e.g. an
    inline ``<table>``). A "complete page text" readout should not dump that raw
    markup: this drops tags, breaks table rows/cells onto new lines, and
    unescapes entities.
    """

    _BLOCK_TAGS = {
        "tr", "p", "div", "li", "h1", "h2", "h3", "h4", "h5",
        "table", "thead", "tbody", "tfoot", "caption", "ul", "ol",
        "section", "blockquote", "pre", "br", "hr",
    }
    _CELL_TAGS = {"td", "th"}

    def __init__(self):
        super().__init__()
        self._parts = []
        self._pending_space = False

    def handle_starttag(self, tag, attrs):
        self._handle(tag)

    def handle_startendtag(self, tag, attrs):
        self._handle(tag)

    def handle_endtag(self, tag):
        if tag.lower() in self._BLOCK_TAGS:
            self._emit_break()

    def _handle(self, tag):
        tag = tag.lower()
        if tag in self._CELL_TAGS:
            self._pending_space = True
        elif tag in self._BLOCK_TAGS:
            self._emit_break()

    def handle_data(self, data):
        text = data.replace("\u00a0", " ").strip()
        if not text:
            return
        if self._pending_space and self._parts and not self._parts[-1].endswith(" "):
            self._parts.append(" ")
        self._pending_space = False
        self._parts.append(text)

    def _emit_break(self):
        if self._parts and self._parts[-1] == " ":
            self._parts.pop()
        if self._parts and self._parts[-1].endswith("\n"):
            return
        self._parts.append("\n")
        self._pending_space = False

    def result(self):
        text = "".join(self._parts)
        lines = [line.rstrip() for line in text.split("\n")]
        out = []
        blank = False
        for line in lines:
            if not line.strip():
                if blank:
                    continue
                blank = True
            else:
                blank = False
            out.append(line)
        return "\n".join(out).strip()


def page_text_to_plain(text):
    """Render raw page text as readable plain text (HTML if present)."""
    if not text:
        return ""
    import html

    if "<" in text and ">" in text:
        parser = _PageTextToPlain()
        try:
            parser.feed(text)
            parser.close()
            return parser.result()
        except Exception:
            return html.unescape(text)
    return html.unescape(text)
