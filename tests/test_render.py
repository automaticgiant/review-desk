"""Draft rendering: GitHub-style markdown (CommonMark plus tables and strikethrough), with raw HTML off and a URL
allowlist on every href and src."""
import re
from html.parser import HTMLParser
from urllib.parse import unquote

import pytest

from review_desk.render import md, md_inline, safe_url


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []  # (tag, {attr: value})

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def tags(h: str):
    p = _Tags()
    p.feed(h)
    return p.tags


def urls(h: str):
    return [a[k] for _, a in tags(h) for k in ("href", "src") if a.get(k) is not None]


# ---- GitHub-style structure ---------------------------------------------------------------------------------

def test_list_right_after_a_line_of_text_is_a_list():
    """(a) GitHub starts a list without a blank line before it; Python-Markdown made a run-on paragraph."""
    h = md("Merge commits:\n- #396: c06dfbd9\n- #591: 1a2b3c4d")
    assert "<p>Merge commits:</p>" in h
    assert re.search(r"<ul>\s*<li>#396: c06dfbd9</li>\s*<li>#591: 1a2b3c4d</li>\s*</ul>", h), h


def test_ordered_list_right_after_a_line_of_text_is_a_list():
    h = md("Steps:\n1. one\n2. two")
    assert "<p>Steps:</p>" in h and re.search(r"<ol>\s*<li>one</li>\s*<li>two</li>\s*</ol>", h), h


def test_two_space_nested_list_nests():
    """(b) Two-space indentation nests, as on GitHub; Python-Markdown needed four and flattened it."""
    h = md("- a\n  - b\n  - c\n- d")
    assert re.search(r"<li>a\s*<ul>\s*<li>b</li>\s*<li>c</li>\s*</ul>\s*</li>\s*<li>d</li>", h), h


def test_nested_ordered_list():
    h = md("1. one\n2. two\n   1. sub")
    assert re.search(r"<li>two\s*<ol>\s*<li>sub</li>\s*</ol>\s*</li>", h), h


def test_table():
    h = md("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in h and "<th>a</th>" in h and "<td>2</td>" in h


def test_table_right_after_a_line_of_text():
    h = md("Results:\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in h and "<td>1</td>" in h, h


def test_strikethrough():
    assert re.search(r"<(s|del)>old</\1> new", md("~~old~~ new"))


def test_fenced_code_keeps_text_and_language():
    h = md("```python\nif a < b:\n    print('x')\n```")
    assert '<pre><code class="language-python">' in h
    assert "if a &lt; b:\n    print(" in h


def test_fenced_code_right_after_a_line_of_text():
    h = md("Run:\n```\nmake test\n```")
    assert "<pre><code>make test\n</code></pre>" in h, h


def test_inline_code_escapes():
    assert "<code>&lt;b&gt;x&lt;/b&gt;</code>" in md("use `<b>x</b>` here")


def test_headings():
    h = md("# One\n## Two\ntext")
    assert "<h1>One</h1>" in h and "<h2>Two</h2>" in h and "<p>text</p>" in h


def test_emphasis_and_links():
    h = md("**bold** and _it_ and [a link](https://example.com/x)")
    assert "<strong>bold</strong>" in h and "<em>it</em>" in h
    assert '<a href="https://example.com/x" rel="noopener noreferrer" target="_blank">a link</a>' in h


def test_bare_url_is_linked():
    h = md("see https://example.com/x for details")
    assert '<a href="https://example.com/x" rel="noopener noreferrer" target="_blank">https://example.com/x</a>' in h


def test_relative_fragment_and_mailto_links_kept():
    h = md("[r](other.md) [f](#item-2) [m](mailto:a@example.com) [s](/p/x/)")
    assert urls(h) == ["other.md", "#item-2", "mailto:a@example.com", "/p/x/"]
    # only web links open a new tab
    assert all("target" not in a for t, a in tags(h) if t == "a")


def test_md_inline_has_no_paragraph():
    assert md_inline("**File** it") == "<strong>File</strong> it"
    assert md_inline("") == ""


# ---- security --------------------------------------------------------------------------------------------------

RAW_HTML = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<iframe src=x></iframe>",
    "text <b onmouseover=alert(1)>bold</b> text",
    "<div>\n\n<script>alert(1)</script>\n\n</div>",
    "<!-- c --><svg onload=alert(1)>",
    "- item <img src=x onerror=alert(1)>",
    "| a |\n|---|\n| <script>x</script> |",
]


@pytest.mark.parametrize("src", RAW_HTML)
def test_raw_html_is_escaped(src):
    for h in (md(src), md_inline(src)):
        assert {t for t, _ in tags(h)} <= {"p", "ul", "li", "table", "thead", "tbody", "tr", "th", "td"}, h
        assert "&lt;" in h


BAD_URLS = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "<java\tscript:alert(1)>",
    "<java\nscript:alert(1)>",
    "&#106;avascript:alert(1)",
    "&#x6A;avascript&#58;alert(1)",
    "javascript&colon;alert(1)",
    "java%09script:alert(1)",
    "\x01javascript:alert(1)",
    "vbscript:msgbox(1)",
    "VBScript:msgbox(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "data:image/svg+xml,<svg onload=alert(1)>",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "chrome://settings",
]


def _scheme(u: str):
    cleaned = re.sub(r"[\x00-\x20\x7f]+", "", unquote(u))
    m = re.match(r"([a-zA-Z][a-zA-Z0-9+.\-]*):", cleaned)
    return m and m.group(1).lower()


@pytest.mark.parametrize("bad", BAD_URLS)
@pytest.mark.parametrize("form", ["[x]({u})", "![x]({u})", "[x][r]\n\n[r]: {u}", "<{u}>", "{u}"])
def test_unsafe_urls_never_reach_href_or_src(bad, form):
    src = form.format(u=bad)
    for h in (md(src), md_inline(src)):
        for u in urls(h):
            assert _scheme(u) in (None, "http", "https", "mailto"), (src, h)


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x", "vbscript:x"])
def test_unsafe_link_keeps_its_text_but_loses_the_url(bad):
    h = md(f"[click]({bad}) ![pic]({bad})")
    assert ">click</a>" in h and 'alt="pic"' in h
    assert urls(h) == [] and "script:" not in h.lower() and "data:" not in h


@pytest.mark.parametrize("u", ["https://example.com/a?b=c#d", "http://example.com", "mailto:x@example.com",
                               "other.md", "./a/b.md", "#frag", "/abs/path", "?q=1", "b/c.md"])
def test_safe_url_allows(u):
    assert safe_url(u)


@pytest.mark.parametrize("u", [b.strip("<>") for b in BAD_URLS if "&" not in b])
def test_safe_url_refuses(u):
    assert not safe_url(u)


def test_link_title_is_escaped():
    h = md('[t](https://example.com "a\\"><script>x</script>")')
    assert "<script" not in h and tags(h)[1][1]["title"] == 'a"><script>x</script>'


def test_txt_like_content_in_code_block_is_not_linked():
    h = md("```\nhttps://example.com/x\n```")
    assert "<a " not in h
