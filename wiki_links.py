"""Small, dependency-free helpers for extracting Wikipedia article links.

The full dump processor deliberately uses this lightweight parser rather than an
API crawl: the English Wikipedia dump contains millions of pages.  It extracts
the first eligible *main-namespace* wikilink from the lead, which defines one
deterministic outgoing edge per page for the classic "Getting to Philosophy"
analysis.
"""

from __future__ import annotations

import html
import re

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_RE = re.compile(r"<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/\s*>", re.DOTALL | re.IGNORECASE)
_HEADING_RE = re.compile(r"^==[^=].*?==\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_WHITESPACE_RE = re.compile(r"\s+")

# A colon is not itself sufficient to reject a title: many main-namespace
# articles have one (for example, ``Star Trek: The Next Generation``). Reject
# only known Wikipedia namespaces and common interwiki/language prefixes.
_NAMESPACE_PREFIXES = frozenset(
    {
        "category",
        "category talk",
        "draft",
        "draft talk",
        "education program",
        "education program talk",
        "file",
        "file talk",
        "gadget",
        "gadget definition",
        "gadget definition talk",
        "gadget talk",
        "help",
        "help talk",
        "image",
        "image talk",
        "media",
        "mediawiki",
        "mediawiki talk",
        "module",
        "module talk",
        "portal",
        "portal talk",
        "special",
        "talk",
        "template",
        "template talk",
        "timedtext",
        "timedtext talk",
        "topic",
        "user",
        "user talk",
        "wikipedia",
        "wikipedia talk",
        "wp",
        "wt",
        "wikt",
        "wikibooks",
        "wikidata",
        "wikinews",
        "wikiquote",
        "wikisource",
        "wikiversity",
        "wikivoyage",
        "wiktionary",
    }
)


def _is_non_article_prefixed_title(title: str) -> bool:
    if ":" not in title:
        return False
    prefix = title.split(":", 1)[0].strip().casefold()
    # Two- and three-letter prefixes are overwhelmingly language/interwiki
    # links in enwiki wikitext. Preserve named colon-bearing article titles.
    return prefix in _NAMESPACE_PREFIXES or bool(re.fullmatch(r"[a-z]{2,3}", prefix))


def normalise_title(value: str) -> str | None:
    """Return a comparable canonical title, or ``None`` for an invalid target."""
    title = html.unescape(value).split("#", 1)[0].replace("_", " ").strip()
    title = _WHITESPACE_RE.sub(" ", title)
    if not title or title.startswith(("/", "#", ":")) or _is_non_article_prefixed_title(title):
        return None

    # MediaWiki's default title rule capitalises the initial character. This is
    # enough to make [[philosophy]] join the dump page titled ``Philosophy``.
    return title[:1].upper() + title[1:]


def _strip_balanced_templates(text: str) -> str:
    """Drop template blocks while tolerating nested braces.

    Templates often contain navigational links that are not part of article
    prose. A full MediaWiki parser would be much slower for a whole dump; this
    linear pass is deliberately conservative and removes only balanced blocks.
    """
    result: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        pair = text[index : index + 2]
        if pair == "{{":
            depth += 1
            index += 2
        elif pair == "}}" and depth:
            depth -= 1
            index += 2
        elif depth == 0:
            result.append(text[index])
            index += 1
        else:
            index += 1
    return "".join(result)


def _strip_parenthetical_text(text: str) -> str:
    """Remove text in balanced parentheses from lead prose.

    The conventional Getting-to-Philosophy definition excludes links inside
    parentheses. Unbalanced parentheses are retained rather than discarding an
    unexpectedly large suffix of an article.
    """
    result: list[str] = []
    depth = 0
    opened_at: list[int] = []
    for char in text:
        if char == "(":
            opened_at.append(len(result))
            depth += 1
            continue
        if char == ")" and depth:
            depth -= 1
            opened_at.pop()
            continue
        if depth == 0:
            result.append(char)

    # An unmatched opening parenthesis should not suppress the rest of the lead.
    if depth:
        return text
    return "".join(result)


def lead_wikitext(wikitext: str) -> str:
    """Remove common non-prose markup and retain the page lead only."""
    text = _COMMENT_RE.sub("", wikitext)
    text = _REF_RE.sub("", text)
    text = _strip_balanced_templates(text)
    heading = _HEADING_RE.search(text)
    if heading:
        text = text[: heading.start()]
    return _strip_parenthetical_text(text)


def extract_first_article_link(wikitext: str) -> str | None:
    """Find the first main-namespace wikilink in lead prose.

    This intentionally does not claim full MediaWiki rendering fidelity. It is
    deterministic, dump-scale friendly, and documents the precise edge rule
    used by the generated graph.
    """
    for match in _LINK_RE.finditer(lead_wikitext(wikitext)):
        raw_target = match.group(1).split("|", 1)[0].strip()
        target = normalise_title(raw_target)
        if target is not None:
            return target
    return None
