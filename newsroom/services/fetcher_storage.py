from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..models import Article, MonitoringTopic
from .ai_relevance import AiRelevanceResult
from .fetcher_shared import (
    CollectedArticle,
    _normalize_url_for_compare,
)
from .text_tools import normalize_text


@dataclass(frozen=True)
class ArticleIdentity:
    normalized_url: str
    content_hash: str


def _build_article_identity(article_data: CollectedArticle) -> ArticleIdentity:
    normalized_url = _normalize_url_for_compare(article_data.url)
    content_text = _normalize_dedupe_text(
        " ".join(part for part in [article_data.title, article_data.content] if part)
    )
    return ArticleIdentity(
        normalized_url=normalized_url[:500],
        content_hash=_sha256_text(content_text) if content_text else "",
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_dedupe_text(value: str) -> str:
    # Для точного дедупа достатньо стабільної нормалізації тексту
    # без окремих словникових правил і тематичних евристик.
    return normalize_text(value)


def _find_duplicate_article(
    topic: MonitoringTopic,
    article_data: CollectedArticle,
    identity: ArticleIdentity,
) -> Article | None:
    # Дублі між різними темами одного користувача не створюємо.
    # Якщо стаття вже була збережена для цього ж джерела, повторно
    # додаємо її лише як оновлення наявного запису, а не як нову новину.
    base_queryset = Article.objects.filter(
        owner=topic.owner,
        source=article_data.source,
    )

    for field_name, value in (
        ("url", article_data.url),
        ("normalized_url", identity.normalized_url),
        ("content_hash", identity.content_hash),
    ):
        if not value:
            continue
        article = base_queryset.filter(**{field_name: value}).order_by("-fetched_at", "-id").first()
        if article:
            return article

    # Для дипломного проєкту залишаємо лише точні збіги.
    # Схожі, але не ідентичні новини далі можуть потрапити в один кластер.
    return None


def _store_topic_article(
    topic: MonitoringTopic,
    article_data: CollectedArticle,
    *,
    fetched_at,
    ai_relevance: AiRelevanceResult | None = None,
    target_status: str = Article.Status.NEW,
) -> tuple[Article, bool, bool]:
    identity = _build_article_identity(article_data)
    duplicate_article = _find_duplicate_article(topic, article_data, identity)

    if duplicate_article:
        article = duplicate_article
        was_restored = article.monitoring_topic_id is None

        _refresh_existing_article(
            article,
            article_data,
            monitoring_topic=topic if was_restored else None,
            reset_cluster=was_restored,
            identity=identity,
            ai_relevance=ai_relevance,
            target_status=target_status,
        )
        return article, False, was_restored

    article = Article.objects.create(
        owner=topic.owner,
        monitoring_topic=topic,
        source=article_data.source,
        title=article_data.title[:255] or "Без назви",
        url=article_data.url,
        normalized_url=identity.normalized_url,
        content_hash=identity.content_hash,
        content=article_data.content,
        published_at=article_data.published_at,
        fetched_at=fetched_at,
        relevance_score=ai_relevance.score if ai_relevance else 0,
        relevance_reason=ai_relevance.reason if ai_relevance else "",
        status=target_status,
    )
    return article, True, False


def _refresh_existing_article(
    article: Article,
    article_data: CollectedArticle,
    *,
    monitoring_topic: MonitoringTopic | None = None,
    reset_cluster: bool = False,
    identity: ArticleIdentity | None = None,
    ai_relevance: AiRelevanceResult | None = None,
    target_status: str = Article.Status.NEW,
) -> None:
    identity = identity or _build_article_identity(article_data)
    article.source = article_data.source
    article.url = article_data.url
    article.normalized_url = identity.normalized_url
    article.content_hash = identity.content_hash
    article.title = article_data.title[:255] or "Без назви"
    article.content = article_data.content
    article.published_at = article_data.published_at
    if monitoring_topic is not None:
        article.monitoring_topic = monitoring_topic
    if reset_cluster:
        article.cluster = None
    if target_status == Article.Status.REJECTED:
        # Ручне схвалення має пріоритет над наступними автоматичними
        # відхиленнями, щоб редактор міг виправити помилку ШІ.
        if not article.manual_relevance_approved and article.status != Article.Status.CLUSTERED:
            article.status = Article.Status.REJECTED
            article.cluster = None
            reset_cluster = True
    elif article.status == Article.Status.REJECTED or not article.cluster_id:
        article.status = Article.Status.NEW

    should_update_ai_fields = bool(ai_relevance) and not (
        ai_relevance
        and not ai_relevance.is_relevant
        and (article.manual_relevance_approved or article.status == Article.Status.CLUSTERED)
    )
    if should_update_ai_fields:
        article.relevance_score = ai_relevance.score
        article.relevance_reason = ai_relevance.reason
    update_fields = [
        "source",
        "url",
        "normalized_url",
        "content_hash",
        "published_at",
        "status",
    ]
    if should_update_ai_fields:
        update_fields.extend(
            [
                "relevance_score",
                "relevance_reason",
            ]
        )
    update_fields.extend(["title", "content"])
    if monitoring_topic is not None:
        update_fields.append("monitoring_topic")
    if reset_cluster:
        update_fields.append("cluster")
    article.save(update_fields=update_fields)
