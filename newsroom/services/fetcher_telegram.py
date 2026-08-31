from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
import re
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from django.utils import timezone

from ..models import MonitoringTopic, Source

TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}

from .fetcher_shared import (
    CancellationCheck,
    CollectedArticle,
    LOCALIZED_MONTH_NAMES,
    _days_to_reach_monitoring_start,
    _http_get,
    _is_older_than_monitoring_period,
    _make_excerpt,
    _meta_content,
    _normalize_url_for_compare,
    _raise_if_cancelled,
)
from .text_tools import strip_html


def _determine_telegram_page_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(50, max(4, days_to_cover + 3))


def _fetch_telegram_source(
    source: Source,
    *,
    topic: MonitoringTopic,
    entry_limit: int = 40,
    cancellation_check: CancellationCheck | None = None,
) -> list[CollectedArticle]:
    preview_url = _normalize_telegram_preview_url(source.url)
    articles: list[CollectedArticle] = []
    seen_article_urls: set[str] = set()
    seen_page_urls: set[str] = set()
    current_url = preview_url
    channel_name = source.name
    page_limit = _determine_telegram_page_limit(topic)
    page_number = 0

    # Telegram не має RSS для каналу, тому рухаємося назад по публічному
    # прев'ю через ?before=<post_id>, доки не дійдемо до межі періоду.
    while current_url and len(articles) < entry_limit and page_number < page_limit:
        _raise_if_cancelled(cancellation_check)
        normalized_page_url = _normalize_url_for_compare(current_url)
        if normalized_page_url in seen_page_urls:
            break
        seen_page_urls.add(normalized_page_url)

        try:
            response = _http_get(current_url, timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            if page_number == 0:
                raise
            break

        page_number += 1
        _raise_if_cancelled(cancellation_check)

        soup = BeautifulSoup(response.text, "html.parser")
        if page_number == 1:
            channel_name = (
                _meta_content(soup, "property", "og:title")
                or _meta_content(soup, "name", "twitter:title")
                or source.name
            )

        page_articles, oldest_post_id, oldest_published_at = _extract_telegram_page_articles(
            source,
            soup,
            channel_name=channel_name,
            preview_url=preview_url,
            seen_article_urls=seen_article_urls,
            remaining_limit=entry_limit - len(articles),
        )
        _raise_if_cancelled(cancellation_check)
        articles.extend(page_articles)

        if not oldest_post_id:
            break
        if oldest_published_at and _is_older_than_monitoring_period(topic, oldest_published_at):
            break

        current_url = _build_telegram_before_url(preview_url, oldest_post_id)

    if articles:
        return articles

    return []


def _extract_telegram_page_articles(
    source: Source,
    soup: BeautifulSoup,
    *,
    channel_name: str,
    preview_url: str,
    seen_article_urls: set[str],
    remaining_limit: int,
) -> tuple[list[CollectedArticle], int | None, datetime | None]:
    articles: list[CollectedArticle] = []
    oldest_post_id: int | None = None
    oldest_published_at: datetime | None = None

    for block in soup.select(".tgme_widget_message_wrap"):
        text_block = block.select_one(".tgme_widget_message_text")
        if not text_block:
            continue

        content = strip_html(str(text_block)).strip()
        if not content:
            continue

        message_link = block.select_one("a.tgme_widget_message_date")
        message_url = message_link.get("href") if message_link else preview_url
        normalized_url = _normalize_url_for_compare(message_url)
        if normalized_url in seen_article_urls:
            continue
        seen_article_urls.add(normalized_url)

        message_id = _extract_telegram_message_id(block, message_url)
        if message_id and (oldest_post_id is None or message_id < oldest_post_id):
            oldest_post_id = message_id

        published_at = _extract_telegram_post_datetime(block)
        if published_at and (oldest_published_at is None or published_at < oldest_published_at):
            oldest_published_at = published_at

        articles.append(
            CollectedArticle(
                source=source,
                title=_build_telegram_post_title(channel_name, content),
                url=message_url,
                content=content,
                published_at=published_at,
            )
        )
        if len(articles) >= remaining_limit:
            break

    return articles, oldest_post_id, oldest_published_at


def _normalize_telegram_preview_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.netloc or "").lower().replace("www.", "")
    if hostname not in TELEGRAM_HOSTS:
        return url

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return url

    if path_parts[0] == "s" and len(path_parts) > 1:
        channel_slug = path_parts[1]
    else:
        channel_slug = path_parts[0]

    return f"https://t.me/s/{channel_slug}"


def _build_telegram_before_url(preview_url: str, before_post_id: int) -> str:
    parsed = urlparse(preview_url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", f"before={before_post_id}", ""))


def _extract_telegram_message_id(block, message_url: str) -> int | None:
    message_node = block.select_one(".tgme_widget_message")
    data_post = ""
    if message_node:
        data_post = message_node.get("data-post", "")
    if not data_post:
        data_post = block.get("data-post", "")

    for value in (data_post, message_url):
        match = re.search(r"/(\d+)(?:\D*$|$)", value or "")
        if match:
            return int(match.group(1))
    return None


def _extract_telegram_post_datetime(block) -> datetime | None:
    time_tag = block.select_one("time")
    if time_tag:
        parsed = _parse_telegram_datetime(time_tag.get("datetime", ""))
        if parsed:
            return parsed

    service_date = block.find_previous(class_="tgme_widget_message_service_date")
    if service_date:
        return _parse_telegram_service_date(service_date.get_text(" ", strip=True))
    return None


def _parse_telegram_service_date(value: str) -> datetime | None:
    normalized = " ".join((value or "").lower().replace(",", " ").split())
    if not normalized:
        return None

    parts = normalized.split()
    day = None
    month = None
    year = None

    for index, part in enumerate(parts):
        if part.isdigit() and 1 <= int(part) <= 31:
            day = int(part)
            if index + 1 < len(parts):
                month = LOCALIZED_MONTH_NAMES.get(parts[index + 1])
            if index + 2 < len(parts) and parts[index + 2].isdigit():
                year = int(parts[index + 2])
            break
        if part in LOCALIZED_MONTH_NAMES and index + 1 < len(parts) and parts[index + 1].isdigit():
            month = LOCALIZED_MONTH_NAMES[part]
            day = int(parts[index + 1])
            if index + 2 < len(parts) and parts[index + 2].isdigit():
                year = int(parts[index + 2])
            break

    if not day or not month:
        return None

    now = timezone.localtime(timezone.now())
    year = year or now.year
    try:
        parsed = timezone.make_aware(datetime(year, month, day), timezone.get_current_timezone())
    except ValueError:
        return None

    # Telegram у роздільниках часто не показує рік. Якщо отримана дата
    # випадково вийшла в майбутньому, значить пост був із попереднього року.
    if parsed > timezone.now() + timedelta(days=1):
        parsed = timezone.make_aware(datetime(year - 1, month, day), timezone.get_current_timezone())
    return parsed


def _build_telegram_post_title(channel_name: str, content: str) -> str:
    cleaned = " ".join(content.split())
    if not cleaned:
        return f"Публікація каналу {channel_name}"

    sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0].strip()
    if len(sentence) > 90:
        return _make_excerpt(sentence, limit=90)
    return sentence


def _parse_telegram_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, dt_timezone.utc)
    return parsed
