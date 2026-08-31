from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from email.utils import parsedate_to_datetime
import gzip
import re
import time
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup
from django.utils import timezone

from ..models import MonitoringTopic, Source
RSS_LINK_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
RSS_FALLBACK_PATHS = ("/feed/", "/feed", "/rss/", "/rss", "/rss.xml", "/feed.xml", "/index.xml")
SITEMAP_REQUEST_TIMEOUT = 60
SITEMAP_FALLBACK_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/post-sitemap.xml",
    "/news-sitemap.xml",
)
DISALLOWED_ARTICLE_SEGMENTS = {
    "about",
    "account",
    "ads",
    "amp",
    "author",
    "authors",
    "cabinet",
    "cart",
    "cdn-cgi",
    "category",
    "categories",
    "comments",
    "contact",
    "content",
    "email-protection",
    "feed",
    "login",
    "page",
    "pages",
    "privacy",
    "profile",
    "register",
    "rss",
    "search",
    "tag",
    "tags",
    "auth",
    "user",
    "users",
    "wp-admin",
    "wp-content",
    "wp-json",
}
MEDIA_FILE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".mp4",
    ".mp3",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
)
LISTING_LINK_SELECTORS = (
    "article a[href]",
    "[class*='post'] a[href]",
    "[class*='news'] a[href]",
    "[class*='entry'] a[href]",
    "[class*='story'] a[href]",
    "[class*='item'] a[href]",
    "main a[href]",
    "h1 a[href]",
    "h2 a[href]",
    "h3 a[href]",
)
ARTICLE_BODY_SELECTORS = (
    "article",
    "[itemprop='articleBody']",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".news-content",
    ".single-content",
    ".content-area",
    "main",
)
PAGINATION_TEXT_HINTS = {
    "next",
    "older",
    "more",
    "далі",
    "наступна",
    "наступні",
    "старіші",
    "старі новини",
    "попередні записи",
    "›",
    "»",
}

from .fetcher_shared import (
    LOCALIZED_MONTH_NAMES,
    CancellationCheck,
    CollectedArticle,
    SitemapArticleCandidate,
    _days_to_reach_monitoring_start,
    _determine_source_fetch_limit,
    _http_get,
    _is_older_than_monitoring_period,
    _make_excerpt,
    _meta_content,
    _normalize_url_for_compare,
    _passes_time_filter,
    _raise_if_cancelled,
    logger,
)
from .text_tools import strip_html


def _looks_like_feed_url(url: str) -> bool:
    lower_url = (url or "").lower()
    return any(marker in lower_url for marker in ("/feed", "/rss", ".xml", "atom"))


def _determine_site_article_scrape_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(1200, max(80, days_to_cover * 25))


def _determine_site_listing_page_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(60, max(4, days_to_cover + 5))


def _determine_site_sitemap_article_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(1200, max(100, days_to_cover * 25))


def _determine_site_sitemap_file_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(40, max(8, days_to_cover))


def _determine_site_sitemap_url_scan_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(12000, max(800, days_to_cover * 250))


def _fetch_site_source(
    source: Source,
    topic: MonitoringTopic,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> list[CollectedArticle]:
    _raise_if_cancelled(cancellation_check)
    response = None
    try:
        response = _http_get(source.url, timeout=20)
        response.raise_for_status()
    except requests.HTTPError as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        # Деякі сайти, зокрема dev.ua, блокують серверні запити на головну сторінку.
        # У такому випадку не зупиняємо весь збір, а пробуємо sitemap та інші
        # альтернативні джерела, які часто лишаються доступними.
        if status_code != 403:
            raise
        logger.warning(
            "Homepage blocked with 403, falling back to sitemap-only scraping.",
            extra={"source_id": source.id, "source_url": source.url},
        )
    _raise_if_cancelled(cancellation_check)

    entry_limit = _determine_source_fetch_limit(topic)
    feed_url = _discover_site_feed_url(source.url, response) if response is not None else None
    feed_articles: list[CollectedArticle] = []
    if feed_url:
        try:
            feed_articles = _fetch_rss_source(
                source,
                feed_url=feed_url,
                entry_limit=entry_limit,
                cancellation_check=cancellation_check,
            )
        except requests.RequestException:
            # Якщо знайдений RSS виявився битим, просто продовжуємо
            # обхід сайту через HTML-сторінки новин.
            feed_articles = []

    sitemap_articles = _fetch_site_sitemap_articles(
        source,
        topic,
        article_limit=_determine_site_sitemap_article_limit(topic),
        cancellation_check=cancellation_check,
    )

    scraped_articles: list[CollectedArticle] = []
    if response is not None:
        listing_url, listing_response = _resolve_site_listing_start(source.url, response, feed_url)
        if listing_response is not None:
            scraped_articles = _fetch_site_archive_articles(
                source,
                topic,
                listing_url=listing_url,
                response=listing_response,
                article_limit=_determine_site_article_scrape_limit(topic),
                page_limit=_determine_site_listing_page_limit(topic),
                cancellation_check=cancellation_check,
            )
    merged_articles = _merge_collected_articles(sitemap_articles, scraped_articles, feed_articles)
    if merged_articles:
        return merged_articles
    return []


def _fetch_rss_source(
    source: Source,
    *,
    feed_url: str | None = None,
    entry_limit: int = 40,
    cancellation_check: CancellationCheck | None = None,
) -> list[CollectedArticle]:
    _raise_if_cancelled(cancellation_check)
    resolved_feed_url = feed_url or source.url
    response = _http_get(resolved_feed_url, timeout=20)
    response.raise_for_status()
    _raise_if_cancelled(cancellation_check)
    feed = feedparser.parse(response.content)

    articles: list[CollectedArticle] = []
    for entry in feed.entries[:entry_limit]:
        _raise_if_cancelled(cancellation_check)
        article_url = entry.get("link")
        if not article_url:
            continue

        title = strip_html(entry.get("title", "Без назви"))
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        content = summary
        if entry.get("content"):
            content = strip_html(entry["content"][0].get("value", summary))

        articles.append(
            CollectedArticle(
                source=source,
                title=title or "Без назви",
                url=article_url,
                content=content,
                published_at=_parse_feed_datetime(entry),
            )
        )
    return articles


def _fetch_site_archive_articles(
    source: Source,
    topic: MonitoringTopic,
    *,
    listing_url: str,
    response: requests.Response,
    article_limit: int,
    page_limit: int,
    cancellation_check: CancellationCheck | None = None,
) -> list[CollectedArticle]:
    # Ця функція проходить сторінки новинного розділу сайту як архів:
    # бере посилання на статті зі списку, відкриває їх по одній і зупиняється,
    # коли досягає ліміту або виходить за часовий період моніторингу.
    articles: list[CollectedArticle] = []
    seen_candidates: set[str] = set()
    visited_listing_urls: set[str] = set()

    current_url = listing_url
    current_response: requests.Response | None = response
    page_number = 1

    # Для сайтів працюємо за тим самим принципом, що й для Telegram:
    # спочатку рухаємося по архіву новин за датами, а вже потім ці
    # матеріали проходять змістову перевірку за темою.
    while current_response is not None and page_number <= page_limit:
        _raise_if_cancelled(cancellation_check)
        visited_listing_urls.add(_normalize_url_for_compare(current_url))
        soup = BeautifulSoup(current_response.text, "html.parser")
        page_candidate_urls: list[str] = []

        for article_url in _extract_article_links_from_listing(soup, current_url, listing_url):
            normalized_url = _normalize_url_for_compare(article_url)
            if normalized_url in seen_candidates:
                continue
            seen_candidates.add(normalized_url)
            page_candidate_urls.append(article_url)
            if len(page_candidate_urls) >= article_limit:
                break

        newest_page_date: datetime | None = None
        for article_url in page_candidate_urls:
            _raise_if_cancelled(cancellation_check)
            url_published_at = _extract_datetime_from_url(article_url)
            if url_published_at:
                if newest_page_date is None or url_published_at > newest_page_date:
                    newest_page_date = url_published_at
                if not topic.matches_published_at(url_published_at):
                    # Багато сайтів, зокрема DEV.UA, мають timestamp прямо в URL.
                    # Якщо дата з URL точно не входить у період, не відкриваємо
                    # зайву сторінку статті й одразу йдемо далі по архіву.
                    continue

            article = _fetch_article_detail(source, article_url)
            if not article:
                continue

            # Якщо дата не витягнулася з самої сторінки, але вона є в URL,
            # підставляємо її як запасний варіант. Так ми не втрачаємо статті,
            # де дата захована лише в адресі матеріалу.
            if url_published_at and not article.published_at:
                article.published_at = url_published_at

            if article.published_at and not url_published_at:
                if newest_page_date is None or article.published_at > newest_page_date:
                    newest_page_date = article.published_at

            if _passes_time_filter(topic, article):
                articles.append(article)

            if len(articles) >= article_limit:
                break

        if len(articles) >= article_limit:
            break

        # Зупиняємо обхід лише тоді, коли навіть найновіший датований матеріал
        # на сторінці старіший за початок періоду. Одна стара промо-стаття
        # або блог серед нових матеріалів не повинні обривати скрейпінг.
        if newest_page_date and _is_older_than_monitoring_period(topic, newest_page_date):
            break

        next_page_url = _find_next_listing_page(soup, current_url, listing_url)
        if not next_page_url:
            next_page_url = _build_fallback_listing_page_url(listing_url, page_number + 1)
        if not next_page_url:
            break

        normalized_next = _normalize_url_for_compare(next_page_url)
        if normalized_next in visited_listing_urls:
            break

        try:
            _raise_if_cancelled(cancellation_check)
            current_response = _http_get(next_page_url, timeout=20)
            current_response.raise_for_status()
        except requests.RequestException:
            break

        current_url = next_page_url
        page_number += 1

    return articles


def _fetch_site_sitemap_articles(
    source: Source,
    topic: MonitoringTopic,
    *,
    article_limit: int,
    cancellation_check: CancellationCheck | None = None,
) -> list[CollectedArticle]:
    _raise_if_cancelled(cancellation_check)
    candidates = _collect_sitemap_article_candidates(
        source,
        topic,
        article_limit=article_limit,
        sitemap_limit=_determine_site_sitemap_file_limit(topic),
        url_scan_limit=_determine_site_sitemap_url_scan_limit(topic),
        cancellation_check=cancellation_check,
    )
    articles: list[CollectedArticle] = []

    # На цьому етапі ми вже маємо URL, які sitemap пов'язує з потрібним
    # періодом. Далі парсимо самі статті й ще раз перевіряємо дату зі сторінки.
    for candidate in candidates[:article_limit]:
        _raise_if_cancelled(cancellation_check)
        article = _fetch_article_detail(source, candidate.url)
        if not article:
            continue
        if not article.published_at and candidate.lastmod:
            article.published_at = candidate.lastmod
        if not _passes_time_filter(topic, article):
            continue
        articles.append(article)

    return articles


def _collect_sitemap_article_candidates(
    source: Source,
    topic: MonitoringTopic,
    *,
    article_limit: int,
    sitemap_limit: int,
    url_scan_limit: int,
    cancellation_check: CancellationCheck | None = None,
) -> list[SitemapArticleCandidate]:
    # Спочатку збираємо лише кандидатів із sitemap, не відкриваючи кожну статтю.
    # Це дешевший етап, який відсікає зайві URL за датою та структурою sitemap,
    # а вже потім окремий крок завантажує самі сторінки статей.
    sitemap_urls = _discover_sitemap_urls(source.url)
    candidates: list[SitemapArticleCandidate] = []
    seen_sitemaps: set[str] = set()
    seen_articles: set[str] = set()

    for sitemap_url in sitemap_urls:
        _raise_if_cancelled(cancellation_check)
        if len(seen_sitemaps) >= sitemap_limit:
            break
        _collect_candidates_from_sitemap(
            source,
            topic,
            sitemap_url=sitemap_url,
            candidates=candidates,
            seen_sitemaps=seen_sitemaps,
            seen_articles=seen_articles,
            article_limit=article_limit,
            sitemap_limit=sitemap_limit,
            url_scan_limit=url_scan_limit,
            depth=0,
            cancellation_check=cancellation_check,
        )
        if len(candidates) >= article_limit:
            break

    return candidates[:article_limit]


def _collect_candidates_from_sitemap(
    source: Source,
    topic: MonitoringTopic,
    *,
    sitemap_url: str,
    candidates: list[SitemapArticleCandidate],
    seen_sitemaps: set[str],
    seen_articles: set[str],
    article_limit: int,
    sitemap_limit: int,
    url_scan_limit: int,
    depth: int,
    cancellation_check: CancellationCheck | None = None,
) -> None:
    _raise_if_cancelled(cancellation_check)
    if depth > 2 or len(candidates) >= article_limit or len(seen_sitemaps) >= sitemap_limit:
        return

    normalized_sitemap_url = _normalize_url_for_compare(sitemap_url)
    if normalized_sitemap_url in seen_sitemaps:
        return
    seen_sitemaps.add(normalized_sitemap_url)

    root = _fetch_sitemap_root(sitemap_url)
    if root is None:
        return

    root_name = _xml_local_name(root.tag)
    if root_name == "sitemapindex":
        child_sitemaps = _extract_sitemap_index_urls(root, sitemap_url)
        for child_sitemap_url in child_sitemaps:
            _raise_if_cancelled(cancellation_check)
            if len(candidates) >= article_limit or len(seen_sitemaps) >= sitemap_limit:
                break
            _collect_candidates_from_sitemap(
                source,
                topic,
                sitemap_url=child_sitemap_url,
                candidates=candidates,
                seen_sitemaps=seen_sitemaps,
                seen_articles=seen_articles,
                article_limit=article_limit,
                sitemap_limit=sitemap_limit,
                url_scan_limit=url_scan_limit,
                depth=depth + 1,
                cancellation_check=cancellation_check,
            )
        return

    if root_name != "urlset":
        return

    dated_candidates: list[SitemapArticleCandidate] = []
    undated_candidates: list[SitemapArticleCandidate] = []

    for index, url_node in enumerate(root):
        _raise_if_cancelled(cancellation_check)
        if index >= url_scan_limit or len(candidates) >= article_limit:
            break
        if _xml_local_name(url_node.tag) != "url":
            continue

        loc = _xml_child_text(url_node, "loc")
        if not loc:
            continue
        normalized_article_url = _normalize_url_for_compare(loc)
        if normalized_article_url in seen_articles:
            continue
        if not _is_probable_article_url(loc, source.url):
            continue

        lastmod = _parse_datetime_candidate(_xml_child_text(url_node, "lastmod"))
        if lastmod:
            if not topic.matches_published_at(lastmod):
                if _is_older_than_monitoring_period(topic, lastmod):
                    break
                continue
            seen_articles.add(normalized_article_url)
            dated_candidates.append(SitemapArticleCandidate(url=loc, lastmod=lastmod))
            if len(candidates) + len(dated_candidates) >= article_limit:
                break
        else:
            seen_articles.add(normalized_article_url)
            undated_candidates.append(SitemapArticleCandidate(url=loc))

        if len(candidates) >= article_limit:
            break

    free_slots = max(0, article_limit - len(candidates))
    candidates.extend(dated_candidates[:free_slots])
    free_slots = max(0, article_limit - len(candidates))
    candidates.extend(undated_candidates[:free_slots])


def _discover_sitemap_urls(site_url: str) -> list[str]:
    parsed = urlparse(site_url)
    if not parsed.scheme or not parsed.netloc:
        return []

    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    sitemap_urls: list[str] = []
    seen_urls: set[str] = set()

    def add_sitemap_url(raw_url: str) -> None:
        absolute_url = urljoin(base_url, raw_url.strip())
        parsed_sitemap = urlparse(absolute_url)
        if parsed_sitemap.scheme not in {"http", "https"} or not parsed_sitemap.netloc:
            return
        normalized_url = _normalize_url_for_compare(absolute_url)
        if normalized_url in seen_urls:
            return
        seen_urls.add(normalized_url)
        sitemap_urls.append(urlunparse((parsed_sitemap.scheme, parsed_sitemap.netloc, parsed_sitemap.path, "", parsed_sitemap.query, "")))

    try:
        robots_response = _http_get(urljoin(base_url, "/robots.txt"), timeout=12)
        if robots_response.ok:
            for line in robots_response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    add_sitemap_url(line.split(":", 1)[1].strip())
    except requests.RequestException:
        pass

    for sitemap_path in SITEMAP_FALLBACK_PATHS:
        add_sitemap_url(urljoin(base_url, sitemap_path))

    return sitemap_urls


def _fetch_sitemap_root(sitemap_url: str):
    try:
        # Карти сайту великих медіа можуть віддаватися повільніше за звичайні сторінки,
        # тому даємо їм довший таймаут, щоб не отримувати хибне "0 матеріалів".
        response = _http_get(sitemap_url, timeout=SITEMAP_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return None

    content = response.content
    if sitemap_url.lower().endswith(".gz"):
        try:
            content = gzip.decompress(content)
        except OSError:
            return None

    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _extract_sitemap_index_urls(root, sitemap_url: str) -> list[str]:
    urls: list[str] = []
    for sitemap_node in root:
        if _xml_local_name(sitemap_node.tag) != "sitemap":
            continue
        loc = _xml_child_text(sitemap_node, "loc")
        if loc:
            urls.append(urljoin(sitemap_url, loc))
    return urls


def _xml_child_text(node, child_name: str) -> str:
    for child in node:
        if _xml_local_name(child.tag) == child_name:
            return (child.text or "").strip()
    return ""


def _xml_local_name(tag: str) -> str:
    return str(tag).split("}", 1)[-1].lower()


def _resolve_site_listing_start(
    source_url: str,
    response: requests.Response,
    feed_url: str | None,
) -> tuple[str, requests.Response | None]:
    # Якщо джерело вказане як домашня сторінка або RSS, намагаємося знайти
    # зручну стартову сторінку саме для списку новин. Це дає змогу далі
    # рухатися по архіву сайту, а не залишатися на feed-адресі або загальній homepage.
    if not _response_looks_like_feed(response) and not _looks_like_feed_url(source_url):
        for listing_url in _find_listing_urls_on_home_page(source_url, response):
            if _normalize_url_for_compare(listing_url) == _normalize_url_for_compare(source_url):
                continue
            try:
                listing_response = _http_get(listing_url, timeout=20)
                listing_response.raise_for_status()
                if not _response_looks_like_feed(listing_response):
                    return listing_url, listing_response
            except requests.RequestException:
                pass
        return source_url, response

    candidate_urls = [
        _extract_feed_home_url(response),
        _strip_feed_suffix(source_url),
    ]

    if feed_url and feed_url != source_url:
        candidate_urls.append(_strip_feed_suffix(feed_url))

    for candidate_url in candidate_urls:
        if not candidate_url:
            continue
        if _normalize_url_for_compare(candidate_url) == _normalize_url_for_compare(source_url):
            continue
        try:
            listing_response = _http_get(candidate_url, timeout=20)
            listing_response.raise_for_status()
        except requests.RequestException:
            continue
        if _response_looks_like_feed(listing_response):
            continue
        return candidate_url, listing_response

    return source_url, None if _response_looks_like_feed(response) else response


def _find_listing_urls_on_home_page(source_url: str, response: requests.Response) -> list[str]:
    parsed_source = urlparse(source_url)
    if (parsed_source.path or "/").rstrip("/") not in {"", "/"}:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    source_host = (parsed_source.netloc or "").lower().replace("www.", "")
    listing_hints = ("стрічка", "новини", "новини it", "news", "articles")
    candidates: list[str] = []
    seen_urls: set[str] = set()

    def add_candidate(raw_url: str) -> None:
        absolute_url = urljoin(source_url, raw_url)
        parsed = urlparse(absolute_url)
        candidate_host = (parsed.netloc or "").lower().replace("www.", "")
        if candidate_host != source_host:
            return
        normalized_url = _normalize_url_for_compare(absolute_url)
        if normalized_url in seen_urls:
            return
        seen_urls.add(normalized_url)
        candidates.append(urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", "")))

    for link_tag in soup.find_all("a", href=True):
        label = link_tag.get_text(" ", strip=True).lower()
        href = link_tag.get("href", "").strip()
        absolute_url = urljoin(source_url, href)
        parsed = urlparse(absolute_url)
        candidate_host = (parsed.netloc or "").lower().replace("www.", "")
        if candidate_host != source_host:
            continue
        if any(hint in label for hint in listing_hints) or re.search(r"/(news|novyny|novini|articles)/?$", parsed.path or ""):
            add_candidate(absolute_url)

    for path in ("/news", "/novyny", "/novini", "/articles"):
        add_candidate(path)

    return candidates


def _merge_collected_articles(*article_groups: list[CollectedArticle]) -> list[CollectedArticle]:
    merged: dict[str, CollectedArticle] = {}
    ordered_urls: list[str] = []

    for article_group in article_groups:
        for article in article_group:
            normalized_url = _normalize_url_for_compare(article.url)
            if normalized_url in merged:
                merged[normalized_url] = _prefer_richer_article(merged[normalized_url], article)
                continue
            merged[normalized_url] = article
            ordered_urls.append(normalized_url)

    return [merged[url] for url in ordered_urls]


def _extract_feed_home_url(response: requests.Response) -> str | None:
    feed = feedparser.parse(response.content)
    home_url = (feed.feed.get("link") or "").strip()
    if not home_url:
        return None
    parsed = urlparse(home_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


def _strip_feed_suffix(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None

    path = parsed.path or "/"
    stripped_path = re.sub(r"(?:/(?:feed|rss))/?$", "/", path, flags=re.I)
    stripped_path = re.sub(r"/(?:feed|rss|index)\.xml$", "/", stripped_path, flags=re.I)
    stripped_path = stripped_path or "/"

    return urlunparse((parsed.scheme, parsed.netloc, stripped_path, "", "", ""))


def _prefer_richer_article(left: CollectedArticle, right: CollectedArticle) -> CollectedArticle:
    left_score = _collected_article_richness(left)
    right_score = _collected_article_richness(right)
    return right if right_score > left_score else left


def _collected_article_richness(article: CollectedArticle) -> int:
    return (
        len(article.content or "")
        + (120 if article.published_at else 0)
    )


def _extract_article_links_from_listing(soup: BeautifulSoup, page_url: str, source_url: str) -> list[str]:
    links: list[str] = []
    seen_urls: set[str] = set()

    for selector in LISTING_LINK_SELECTORS:
        for link_tag in soup.select(selector):
            candidate_url = _normalize_article_candidate_url(link_tag.get("href", ""), page_url)
            if not candidate_url or not _is_probable_article_url(candidate_url, source_url):
                continue
            normalized_url = _normalize_url_for_compare(candidate_url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            links.append(candidate_url)

    for link_tag in soup.find_all("a", href=True):
        candidate_url = _normalize_article_candidate_url(link_tag.get("href", ""), page_url)
        if not candidate_url or not _is_probable_article_url(candidate_url, source_url):
            continue
        normalized_url = _normalize_url_for_compare(candidate_url)
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        links.append(candidate_url)

    return links


def _normalize_article_candidate_url(href: str, page_url: str) -> str:
    if not href:
        return ""
    absolute_url = urljoin(page_url, href.strip())
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def _extract_datetime_from_url(url: str) -> datetime | None:
    parsed = urlparse(url or "")
    # Деякі новинні сайти додають Unix timestamp у slug статті.
    # Це дозволяє швидко відкидати матеріали поза періодом без відкриття сторінки.
    for match in re.finditer(r"(?<!\d)(1[0-9]{9})(?!\d)", parsed.path or ""):
        try:
            parsed_datetime = datetime.fromtimestamp(int(match.group(1)), tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        if 2000 <= parsed_datetime.year <= timezone.now().year + 1:
            return parsed_datetime
    return None


def _is_probable_article_url(candidate_url: str, source_url: str) -> bool:
    source_parsed = urlparse(source_url)
    candidate_parsed = urlparse(candidate_url)
    source_host = (source_parsed.netloc or "").lower().replace("www.", "")
    candidate_host = (candidate_parsed.netloc or "").lower().replace("www.", "")
    if not candidate_host or candidate_host != source_host:
        return False

    normalized_source_url = _normalize_url_for_compare(source_url)
    normalized_candidate_url = _normalize_url_for_compare(candidate_url)
    if normalized_candidate_url == normalized_source_url:
        return False

    path = (candidate_parsed.path or "").lower().rstrip("/")
    if not path or path == "/":
        return False
    if path.endswith(MEDIA_FILE_EXTENSIONS):
        return False

    path_segments = [segment for segment in path.split("/") if segment]
    if not path_segments:
        return False
    if any(segment in DISALLOWED_ARTICLE_SEGMENTS for segment in path_segments):
        return False
    if any(re.fullmatch(r"page-\d+", segment) or re.fullmatch(r"page\d+", segment) for segment in path_segments):
        return False

    last_segment = path_segments[-1]
    if len(path_segments) >= 2:
        return True

    return "-" in last_segment or "_" in last_segment or len(last_segment) >= 16


def _find_next_listing_page(soup: BeautifulSoup, current_url: str, source_url: str) -> str | None:
    source_host = (urlparse(source_url).netloc or "").lower().replace("www.", "")
    for link_tag in soup.find_all("a", href=True):
        href = link_tag.get("href", "").strip()
        if not href:
            continue

        absolute_url = urljoin(current_url, href)
        parsed = urlparse(absolute_url)
        candidate_host = (parsed.netloc or "").lower().replace("www.", "")
        if candidate_host != source_host:
            continue
        if _is_probable_article_url(absolute_url, source_url):
            continue

        rel_values = {value.lower() for value in (link_tag.get("rel") or [])}
        label_text = " ".join(
            filter(
                None,
                [
                    link_tag.get_text(" ", strip=True).lower(),
                    (link_tag.get("aria-label") or "").strip().lower(),
                    " ".join(link_tag.get("class", [])).lower(),
                ],
            )
        )
        if "next" in rel_values or any(hint in label_text for hint in PAGINATION_TEXT_HINTS):
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
    return None


def _build_fallback_listing_page_url(source_url: str, page_number: int) -> str | None:
    if page_number <= 1:
        return source_url

    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        return None

    clean_path = (parsed.path or "/").rstrip("/")
    if not clean_path:
        clean_path = "/"

    page_match = re.search(r"/page/(\d+)$", clean_path)
    if page_match:
        clean_path = re.sub(r"/page/\d+$", "", clean_path)

    if clean_path == "/":
        page_path = f"/page/{page_number}/"
    else:
        page_path = f"{clean_path}/page/{page_number}/"

    return urlunparse((parsed.scheme, parsed.netloc, page_path, "", "", ""))


def _fetch_article_detail(source: Source, article_url: str) -> CollectedArticle | None:
    try:
        response = _http_get(article_url, timeout=20)
        response.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    title = _extract_article_title(soup, fallback=source.name)
    content = _extract_article_content(soup)
    summary = (
        _meta_content(soup, "name", "description")
        or _meta_content(soup, "property", "og:description")
        or _make_excerpt(content, limit=280)
    )
    published_at = _extract_article_datetime(soup)
    if not title or (not content and not summary):
        return None

    return CollectedArticle(
        source=source,
        title=title[:255],
        url=article_url,
        content=content or summary,
        published_at=published_at,
    )


def _extract_article_title(soup: BeautifulSoup, *, fallback: str) -> str:
    title = (
        _meta_content(soup, "property", "og:title")
        or _meta_content(soup, "name", "twitter:title")
        or _meta_content(soup, "name", "title")
    )
    if title:
        return strip_html(title)

    heading = soup.find("h1")
    if heading:
        return strip_html(str(heading))

    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return fallback


def _extract_article_content(soup: BeautifulSoup) -> str:
    for selector in ARTICLE_BODY_SELECTORS:
        container = soup.select_one(selector)
        if not container:
            continue

        paragraphs = _extract_text_paragraphs(container.find_all(["p", "li"]))
        if paragraphs:
            return "\n\n".join(paragraphs)

    return "\n\n".join(_extract_text_paragraphs(soup.find_all("p")))


def _extract_text_paragraphs(tags) -> list[str]:
    paragraphs: list[str] = []
    seen_texts: set[str] = set()

    for tag in tags:
        text = strip_html(str(tag)).strip()
        if len(text) < 35:
            continue
        normalized_text = " ".join(text.split())
        if normalized_text in seen_texts:
            continue
        seen_texts.add(normalized_text)
        paragraphs.append(normalized_text)
        if len(paragraphs) >= 40:
            break

    return paragraphs


def _extract_article_datetime(soup: BeautifulSoup) -> datetime | None:
    for attr_name, attr_value in (
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("name", "article:published_time"),
        ("name", "publishdate"),
        ("name", "pubdate"),
        ("itemprop", "datePublished"),
    ):
        parsed = _parse_datetime_candidate(_meta_content(soup, attr_name, attr_value))
        if parsed:
            return parsed

    for time_tag in soup.find_all("time"):
        parsed = _parse_datetime_candidate(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
        if parsed:
            return parsed

    for tag in soup.find_all(attrs={"class": re.compile(r"(date|time|publish|posted|meta)", re.I)}):
        parsed = _parse_datetime_candidate(tag.get("datetime") or tag.get_text(" ", strip=True))
        if parsed:
            return parsed

    return None


def _parse_datetime_candidate(value: str) -> datetime | None:
    raw_value = (value or "").strip()
    if not raw_value:
        return None

    normalized_value = raw_value.replace("Z", "+00:00")
    normalized_value = normalized_value.replace("\u00a0", " ")
    normalized_value = re.sub(r"\s+", " ", normalized_value).strip()

    localized_date = _parse_localized_month_datetime(normalized_value)
    if localized_date:
        return localized_date

    # Деякі сайти можуть виводити дату всередині довшого
    # текстового рядка: "20 квітня 2026, 10:54 2026-04-20".
    # Тому окремо дістаємо ISO-дату з такого рядка, якщо повний текст
    # стандартними парсерами не читається.
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})(?:[T\s](\d{2}:\d{2}(?::\d{2})?))?", normalized_value)
    if iso_match:
        iso_value = " ".join(part for part in iso_match.groups() if part)
        parsed_iso = _parse_datetime_candidate(iso_value) if iso_value != normalized_value else None
        if parsed_iso:
            return parsed_iso

    if normalized_value.isdigit():
        timestamp = int(normalized_value)
        if len(normalized_value) == 13:
            timestamp = timestamp / 1000
        if len(normalized_value) in {10, 13}:
            return datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)

    for candidate in (
        normalized_value,
        normalized_value.replace(" UTC", ""),
        normalized_value.replace(" GMT", ""),
    ):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None
        if parsed:
            if timezone.is_naive(parsed):
                return timezone.make_aware(parsed, timezone.get_current_timezone())
            return parsed

    try:
        parsed = parsedate_to_datetime(normalized_value)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed:
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    for date_format in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M",
    ):
        try:
            parsed = datetime.strptime(normalized_value, date_format)
        except ValueError:
            continue
        return timezone.make_aware(parsed, timezone.get_current_timezone())

    return None


def _parse_localized_month_datetime(value: str) -> datetime | None:
    month_names = "|".join(re.escape(name) for name in sorted(LOCALIZED_MONTH_NAMES, key=len, reverse=True))
    match = re.search(
        rf"\b(\d{{1,2}})\s+({month_names})\s+(\d{{4}})(?:\s*(?:,|о)?\s*(\d{{1,2}}):(\d{{2}}))?",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    day = int(match.group(1))
    month = LOCALIZED_MONTH_NAMES.get(match.group(2).lower())
    year = int(match.group(3))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    if not month:
        return None

    try:
        parsed = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return timezone.make_aware(parsed, timezone.get_current_timezone())


def _discover_site_feed_url(source_url: str, response: requests.Response) -> str | None:
    if _response_looks_like_feed(response):
        return source_url

    soup = BeautifulSoup(response.text, "html.parser")
    discovered_link = _extract_feed_link_from_html(soup, source_url)
    if discovered_link:
        return discovered_link

    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        return None

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    checked_urls: set[str] = set()
    for path in RSS_FALLBACK_PATHS:
        candidate_url = urljoin(base_url, path)
        if candidate_url in checked_urls:
            continue
        checked_urls.add(candidate_url)
        if _probe_feed_url(candidate_url):
            return candidate_url
    return None


def _extract_feed_link_from_html(soup: BeautifulSoup, source_url: str) -> str | None:
    for link_tag in soup.find_all("link", href=True):
        rel_values = {value.lower() for value in (link_tag.get("rel") or [])}
        link_type = (link_tag.get("type") or "").lower().strip()
        href = link_tag.get("href", "").strip()
        if "alternate" not in rel_values or not href:
            continue
        if link_type in RSS_LINK_TYPES or "rss" in href.lower() or "feed" in href.lower():
            return urljoin(source_url, href)
    return None


def _probe_feed_url(candidate_url: str) -> bool:
    try:
        response = _http_get(candidate_url, timeout=12)
        response.raise_for_status()
    except requests.RequestException:
        return False
    return _response_looks_like_feed(response)


def _response_looks_like_feed(response: requests.Response) -> bool:
    content_type = (response.headers.get("Content-Type") or "").lower()
    response_text = response.text.lower()
    if any(token in content_type for token in ("rss", "atom", "xml")):
        return True
    return "<rss" in response_text or "<feed" in response_text or "<rdf:rdf" in response_text


def _parse_feed_datetime(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    timestamp = time.mktime(parsed)
    return datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
