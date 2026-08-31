from __future__ import annotations

from django.contrib.auth.models import User
from django.utils import timezone

from ..models import ActivityEvent, Article, MonitoringTopic, Source
from .ai_relevance import check_article_ai_relevance
from .fetcher_shared import (
    CancellationCheck,
    CollectedArticle,
    MonitoringCancelled,
    _build_cancellation_check,
    _determine_source_fetch_limit,
    _filter_articles_by_time,
    logger,
)
from .fetcher_site import _fetch_site_source
from .fetcher_storage import _store_topic_article
from .fetcher_telegram import _fetch_telegram_source
from .relevance import matches_monitoring_topic


def get_topic_monitoring_sources(topic: MonitoringTopic):
    selected_sources = topic.sources.filter(owner=topic.owner).exclude(status=Source.Status.ARCHIVED).order_by("name")
    if selected_sources.exists():
        return selected_sources

    # Якщо для теми не вибрано жодного активного джерела вручну,
    # використовуємо всі активні джерела користувача.
    return Source.objects.filter(owner=topic.owner).exclude(status=Source.Status.ARCHIVED).order_by("name")


def run_monitoring_for_topic(topic: MonitoringTopic, actor: User | None = None) -> dict[str, int | str]:
    selected_sources = get_topic_monitoring_sources(topic)
    if not selected_sources.exists():
        now = timezone.now()
        topic.last_run_at = now
        topic.save(update_fields=["last_run_at"])
        ActivityEvent.log(
            owner=topic.owner,
            description=f"Моніторинг теми «{topic.topic}» не запущено: немає активних джерел.",
        )
        return {
            "fetched_total": 0,
            "scanned": 0,
            "ai_checked": 0,
            "ai_rejected": 0,
            "ai_rejected_saved": 0,
            "relevant": 0,
            "created": 0,
            "failed_sources": 0,
            "restored": 0,
            "message": "Моніторинг не виконано, бо для теми немає жодного активного джерела.",
        }

    fetched_total = 0
    scanned = 0
    created = 0
    relevant = 0
    ai_checked = 0
    ai_rejected = 0
    ai_rejected_saved = 0
    failed_sources = 0
    restored = 0
    now = timezone.now()
    cancellation_check = _build_cancellation_check(topic.id)
    cancelled = cancellation_check()

    for source in selected_sources:
        if cancelled or cancellation_check():
            cancelled = True
            break

        try:
            collected_articles = _collect_source_articles(source, topic, cancellation_check=cancellation_check)
        except MonitoringCancelled:
            cancelled = True
            break
        except Exception as error:
            logger.exception(
                "Не вдалося зібрати матеріали з джерела.",
                extra={
                    "topic_id": topic.id,
                    "source_id": source.id,
                    "source_url": source.url,
                },
            )
            error_type = type(error).__name__
            error_text = str(error).strip().replace("\n", " ")
            http_status = getattr(getattr(error, "response", None), "status_code", None)
            if http_status:
                error_summary = f"{error_type} HTTP {http_status}"
            else:
                error_summary = error_type
            if error_text:
                error_summary = f"{error_summary}: {error_text[:120]}"
            ActivityEvent.log(
                owner=topic.owner,
                description=f"Джерело {source.name}: помилка збору новин ({error_summary}).",
            )
            failed_sources += 1
            continue

        source.last_fetched_at = now
        source.status = Source.Status.ACTIVE
        source.save(update_fields=["last_fetched_at", "status"])
        fetched_total += len(collected_articles)

        if not collected_articles:
            # Якщо джерело не повернуло жодного матеріалу, окремо фіксуємо це в журналі.
            # Так легше зрозуміти, де саме зникають новини: у зборі, у фільтрі дати чи в AI.
            ActivityEvent.log(
                owner=topic.owner,
                description=f"Джерело {source.name}: не знайдено матеріалів у потрібному періоді.",
            )

        for article_data in collected_articles:
            if cancellation_check():
                cancelled = True
                break
            # У звіті "перевірено" рахуємо тільки ті матеріали, які реально
            # потрапили у вибраний часовий проміжок і були перевірені за темою.
            scanned += 1
            if not _matches_monitoring_topic(topic, article_data):
                continue

            ai_checked += 1
            ai_relevance = check_article_ai_relevance(topic, article_data)
            if not ai_relevance.is_relevant:
                ai_rejected += 1
                _article, was_created, was_restored = _store_topic_article(
                    topic,
                    article_data,
                    fetched_at=now,
                    ai_relevance=ai_relevance,
                    target_status=Article.Status.REJECTED,
                )
                if was_created or was_restored:
                    ai_rejected_saved += 1
                continue

            relevant += 1
            article, was_created, was_restored = _store_topic_article(
                topic,
                article_data,
                fetched_at=now,
                ai_relevance=ai_relevance,
            )
            if was_created:
                created += 1
            elif was_restored:
                restored += 1

        if cancelled:
            break

    topic.last_run_at = now
    topic.save(update_fields=["last_run_at"])

    if cancelled:
        result_message_start = (
            f"Моніторинг зупинено користувачем. До зупинки зібрано за вибраний період {scanned} матеріалів, "
        )
    else:
        result_message_start = f"Моніторинг завершено. Зібрано за вибраний період {scanned} матеріалів, "

    result_message = (
        result_message_start
        + f"ШІ відхилив {ai_rejected}, збережено для перевірки {ai_rejected_saved}, "
        + f"знайдено {relevant} релевантних, додано {created} нових, "
        + f"джерел з помилкою {failed_sources}."
    )

    ActivityEvent.log(
        owner=topic.owner,
        description=(
            f"{'Зупинено' if cancelled else 'Завершено'} моніторинг теми: {topic.topic}. "
            f"Проскановано матеріалів {scanned}, релевантних {relevant}, "
            f"відхилено ШІ {ai_rejected}, додано нових {created}."
        ),
    )

    return {
        "fetched_total": fetched_total,
        "scanned": scanned,
        "ai_checked": ai_checked,
        "ai_rejected": ai_rejected,
        "ai_rejected_saved": ai_rejected_saved,
        "relevant": relevant,
        "created": created,
        "failed_sources": failed_sources,
        "restored": restored,
        "message": result_message,
    }


def _collect_source_articles(
    source: Source,
    topic: MonitoringTopic,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> list[CollectedArticle]:
    if source.type == Source.SourceType.TELEGRAM:
        articles = _fetch_telegram_source(
            source,
            topic=topic,
            entry_limit=_determine_source_fetch_limit(topic),
            cancellation_check=cancellation_check,
        )
    else:
        articles = _fetch_site_source(source, topic, cancellation_check=cancellation_check)

    return _filter_articles_by_time(topic, articles)


def _matches_monitoring_topic(topic: MonitoringTopic, article_data: CollectedArticle) -> bool:
    # Первинний локальний відбір навмисно спрощено:
    # шукаємо лише прямі збіги назви теми та ключових слів у заголовку й тексті.
    # Якщо є базовий збіг, далі зміст уже точно перевіряє ai_relevance.
    return matches_monitoring_topic(
        topic,
        title=article_data.title,
        content=article_data.content,
    )
