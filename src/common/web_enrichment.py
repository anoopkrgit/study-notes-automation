"""
web_enrichment.py -- the bounded web-search / web-fetch machinery, shared BY
IMPORT (not by copy) between the Stage 2 implementations that offer web
enrichment to the model: src/agents/tools.py (LangGraph) and
src/direct_api/stage2_api.py (the direct-SDK agentic loop). Originally written
once inside src/agents/tools.py; moved here so a second implementation can use
it without a second copy that would quietly drift -- same posture as
src/common/skill_retro.py and src/common/skill_package.py.

Everything here is implementation-agnostic: plain functions over a query /
url / list-of-urls, plus the domain whitelist. The per-implementation tool
SCHEMAS (what the model sees) stay in each implementation, because their
shapes differ (agents injects a chapter_dir arg every tool must accept;
direct_api's tools don't take one) -- only the behavior is shared.

The claude_cli_subprocess implementation deliberately does NOT use this: it
grants Claude Code's own native WebSearch/WebFetch tools with domain-scoped
permission rules, so there's no Python tool to share there (see
docs/web-enrichment-plan.md).
"""

import json
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import settings as config
from src.func_tools_and_utils import logger

try:
    from ddgs import DDGS
except ImportError:
    # Assume it's installed in real deployments (it's in requirements.txt);
    # degrade gracefully to a clear error rather than an import crash if not.
    DDGS = None


class _HTMLToText(HTMLParser):
    """A very small HTML-to-text extractor for web_fetch -- pulls readable
    body text, drops <script>/<style>/<head> noise, and inserts newlines at
    block boundaries. Deliberately dependency-free (stdlib html.parser), not
    a full readability engine."""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.in_body = False
        self.ignore_tags = {'script', 'style', 'head', 'meta', 'link'}
        self.current_ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            self.in_body = True
        if tag in self.ignore_tags:
            self.current_ignore += 1
        if tag in {'p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'}:
            self.text_parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.ignore_tags and self.current_ignore > 0:
            self.current_ignore -= 1
        if tag == 'body':
            self.in_body = False

    def handle_data(self, data):
        if self.in_body and self.current_ignore == 0:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned + " ")

    def get_text(self):
        return "".join(self.text_parts).strip()


def is_domain_allowed(domain: str) -> bool:
    """Single source of truth for the web-enrichment domain whitelist --
    config.WEB_SEARCH_ALLOWED_DOMAINS. `domain` matches if it equals an
    allowed entry or is a subdomain of one."""
    return any(domain == d or domain.endswith("." + d) for d in config.WEB_SEARCH_ALLOWED_DOMAINS)


def web_search(query: str, allowed_domains: list) -> str:
    """Search the web for `query`, constrained to `allowed_domains` (each of
    which must itself be inside config.WEB_SEARCH_ALLOWED_DOMAINS). Returns a
    JSON string: either a list of {url,title,snippet} results, or an
    {"error": ...} object."""
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        logger.info(f"[web_search] DEV MODE mock search for: {query}")
        return json.dumps([{"url": "https://en.wikipedia.org/wiki/Mock", "title": "Mock", "snippet": "Mock search result."}])

    if not DDGS:
        return json.dumps({"error": "ddgs library not installed. Web search unavailable."})

    validated_domains = [d for d in allowed_domains if is_domain_allowed(d)]

    if not validated_domains:
        return json.dumps({"error": f"None of the requested domains are in the whitelist: {config.WEB_SEARCH_ALLOWED_DOMAINS}"})

    site_query = " OR ".join([f"site:{d}" for d in validated_domains])
    full_query = f"{query} ({site_query})"

    logger.info(f"[web_search] Searching: {full_query}")
    try:
        results = DDGS().text(full_query, max_results=5)
        # DuckDuckGo sometimes returns empty lists if no results
        if not results:
            return json.dumps([])
        return json.dumps([{"url": r.get('href'), "title": r.get('title'), "snippet": r.get('body')} for r in results])
    except Exception as e:
        logger.error(f"[web_search] Error: {e}")
        return json.dumps({"error": f"Search failed: {str(e)}"})


def web_fetch(url: str) -> str:
    """Fetch and return the readable text content of `url` (which must resolve
    to an allowed domain). Returns the extracted text, or a JSON {"error": ...}
    string on rejection/failure."""
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        logger.info(f"[web_fetch] DEV MODE mock fetch for: {url}")
        return "Mock content for web fetch."

    # Validate domain (and scheme -- urllib also understands file:// etc.,
    # which must never reach urlopen() here).
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not is_domain_allowed(parsed.netloc):
        return json.dumps({"error": f"Domain {parsed.netloc} is not in the whitelist: {config.WEB_SEARCH_ALLOWED_DOMAINS}"})

    logger.info(f"[web_fetch] Fetching: {url}")
    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            # urlopen follows redirects with no domain re-check of its own --
            # re-validate the URL actually reached before trusting the body,
            # so an allowed page can't silently redirect us off-whitelist.
            final_domain = urllib.parse.urlparse(response.url).netloc
            if not is_domain_allowed(final_domain):
                return json.dumps({"error": f"Redirected outside the allowed domains (to {final_domain}); refusing to read the response."})
            # Cap bytes read, not just the text after decoding -- an
            # allowed domain serving an unexpectedly huge page shouldn't be
            # read into memory in full first.
            html = response.read(2_000_000).decode('utf-8', errors='ignore')
            parser = _HTMLToText()
            parser.feed(html)
            text = parser.get_text()

            # Cap the length to avoid blowing up the context window
            max_len = 15000
            if len(text) > max_len:
                text = text[:max_len] + "\n\n...[CONTENT TRUNCATED]..."
            return text
    except Exception as e:
        logger.error(f"[web_fetch] Error fetching {url}: {e}")
        return json.dumps({"error": f"Fetch failed: {str(e)}"})


def write_web_sources_manifest(target_dir: Path, web_sources: list) -> None:
    """Best-effort audit trail for bounded web enrichment: write
    target_dir/config.WEB_SOURCES listing the pages actually fetched this run.
    Built from the real tool executions the caller collected, not the model's
    self-report. No-op when nothing was fetched; never raises."""
    if not web_sources:
        return
    try:
        lines = ["Web enrichment audit trail (docs/web-enrichment-plan.md).",
                 "Built from the actual generation tool executions, not self-reported.",
                 "", f"Pages fetched ({len(web_sources)}):"]
        lines += [f"  - {u}" for u in web_sources] or ["  (none)"]
        (target_dir / config.WEB_SOURCES).write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"Wrote {config.WEB_SOURCES} ({len(web_sources)} fetch(es)).")
    except Exception as e:
        logger.warning(f"Could not write {config.WEB_SOURCES} audit trail: {e}")
