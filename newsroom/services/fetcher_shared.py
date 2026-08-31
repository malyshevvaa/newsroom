from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from django.utils import timezone

from ..models import MonitoringTopic, Source

REQUEST_HEADERS = {
    "User-Agent": "NewsRoom-Diploma/1.0 (+Django demo project)",
}

CancellationCheck = Callable[[], bool]
logger = logging.getLogger(__name__)
_http_state = threading.local()


class MonitoringCancelled(Exception):
    """Службовий виняток для м'якої зупинки фонового моніторингу."""


LOCALIZED_MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "січня": 1,
    "января": 1,
    "february": 2,
    "feb": 2,
    "лютого": 2,
    "февраля": 2,
    "march": 3,
    "mar": 3,
    "березня": 3,
    "марта": 3,
    "april": 4,
    "apr": 4,
    "квітня": 4,
    "апреля": 4,
    "may": 5,
    "травня": 5,
    "мая": 5,
    "june": 6,
    "jun": 6,
    "червня": 6,
    "июня": 6,
    "july": 7,
    "jul": 7,
    "липня": 7,
    "июля": 7,
    "august": 8,
    "aug": 8,
    "серпня": 8,
    "августа": 8,
    "september": 9,
    "sep": 9,
    "вересня": 9,
    "сентября": 9,
    "october": 10,
    "oct": 10,
    "жовтня": 10,
    "октября": 10,
    "november": 11,
    "nov": 11,
    "листопада": 11,
    "ноября": 11,
    "december": 12,
    "dec": 12,
    "грудня": 12,
    "декабря": 12,
}
TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "yclid",
}
TRACKING_QUERY_PREFIXES = ("utm_",)


@dataclass
class CollectedArticle:
    source: Source
    title: str
    url: str
    content: str = ""
    published_at: datetime | None = None


@dataclass(frozen=True)
class SitemapArticleCandidate:
    url: str
    lastmod: datetime | None = None


def _get_http_session() -> requests.Session:
    session = getattr(_http_state, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _http_state.session = session
    return session


def _http_get(url: str, *, timeout: int = 20) -> requests.Response:
    # requests автоматично читає HTTP_PROXY/HTTPS_PROXY з оточення.
    # У локальному запуску ці змінні можуть вказувати на службовий proxy
    # Codex/термінала 127.0.0.1:9, через що падають усі джерела одразу.
    # Сесію тримаємо на рівні потоку, щоб не створювати нове HTTP-з'єднання
    # на кожен окремий запит під час великого моніторингу.
    session = _get_http_session()
    return session.get(url, timeout=timeout, headers=REQUEST_HEADERS)


def _build_cancellation_check(topic_id: int) -> CancellationCheck:
    last_check = {"time": 0.0, "cancelled": False}

    def check() -> bool:
        # Не звертаємося до бази на кожній статті без паузи: достатньо
        # перевіряти прапорець приблизно раз на секунду.
        current_time = time.monotonic()
        if current_time - last_check["time"] < 0.8:
            return bool(last_check["cancelled"])

        last_check["time"] = current_time
        last_check["cancelled"] = MonitoringTopic.objects.filter(
            id=topic_id,
            cancel_requested=True,
        ).exists()
        return bool(last_check["cancelled"])

    return check


def _raise_if_cancelled(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check and cancellation_check():
        raise MonitoringCancelled


def _normalize_url_for_compare(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]

    path = parsed.path.rstrip("/") or "/"
    if path.endswith("/amp"):
        path = path[:-4] or "/"

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.lower()
        if normalized_key in TRACKING_QUERY_PARAMS:
            continue
        if any(normalized_key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_items.append((normalized_key, value))
    normalized_query = urlencode(sorted(query_items))

    return urlunparse(
        (
            (parsed.scheme or "https").lower(),
            host,
            path,
            "",
            normalized_query,
            "",
        )
    )

def _passes_time_filter(topic: MonitoringTopic, article_data: CollectedArticle) -> bool:
    if not article_data.published_at:
        # Якщо не вдалося визначити дату, матеріал не можна чесно
        # віднести до конкретного часового проміжку моніторингу.
        return False
    return topic.matches_published_at(article_data.published_at)


def _filter_articles_by_time(topic: MonitoringTopic, articles: list[CollectedArticle]) -> list[CollectedArticle]:
    # Часова перевірка виконується до змістової, бо дата є простішим
    # і швидшим критерієм відбору для новинного агрегатора.
    return [article for article in articles if _passes_time_filter(topic, article)]


def _determine_source_fetch_limit(topic: MonitoringTopic) -> int:
    days_to_cover = _days_to_reach_monitoring_start(topic)
    return min(1000, max(120, days_to_cover * 25))


def _days_to_reach_monitoring_start(topic: MonitoringTopic) -> int:
    # Для архівного скрейпінгу важливий не тільки розмір періоду, а й те,
    # наскільки далеко від сьогодні знаходиться його початок. Наприклад,
    # точна дата два тижні тому потребує обходу приблизно двох тижнів архіву.
    today = timezone.localdate()

    if topic.time_window == MonitoringTopic.TimeWindow.DAY:
        return 1
    if topic.time_window == MonitoringTopic.TimeWindow.WEEK:
        return 7
    if topic.time_window == MonitoringTopic.TimeWindow.MONTH:
        return 31
    if topic.time_window == MonitoringTopic.TimeWindow.EXACT_DATE and topic.exact_date:
        return max(1, (today - topic.exact_date).days + 1)
    if topic.time_window == MonitoringTopic.TimeWindow.DATE_RANGE and topic.date_from:
        return max(1, (today - topic.date_from).days + 1)
    return 7


def _monitoring_period_start(topic: MonitoringTopic) -> datetime:
    current_timezone = timezone.get_current_timezone()
    if topic.time_window == MonitoringTopic.TimeWindow.EXACT_DATE and topic.exact_date:
        return timezone.make_aware(datetime.combine(topic.exact_date, datetime.min.time()), current_timezone)
    if topic.time_window == MonitoringTopic.TimeWindow.DATE_RANGE and topic.date_from:
        return timezone.make_aware(datetime.combine(topic.date_from, datetime.min.time()), current_timezone)
    return topic.time_window_start()


def _is_older_than_monitoring_period(topic: MonitoringTopic, published_at: datetime) -> bool:
    period_start = _monitoring_period_start(topic)
    if timezone.is_naive(published_at):
        published_at = timezone.make_aware(published_at, timezone.get_current_timezone())
    return published_at < period_start


def _make_excerpt(text: str, *, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    shortened = normalized[:limit].rsplit(" ", 1)[0].strip()
    return f"{shortened}..." if shortened else normalized[:limit].strip()


def _meta_content(soup, attr_name: str, attr_value: str) -> str:
    tag = soup.find("meta", attrs={attr_name: attr_value})
    return tag.get("content", "").strip() if tag else ""
