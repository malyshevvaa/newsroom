from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from openai import OpenAI

from ..models import MonitoringTopic
from .relevance import (
    LOCAL_RELEVANCE_THRESHOLD,
    build_topic_terms,
    calculate_local_relevance_score,
)

RELEVANCE_REASON_MAX_CHARS = 90

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiRelevanceResult:
    is_relevant: bool
    score: int
    reason: str


def check_article_ai_relevance(topic: MonitoringTopic, article_data) -> AiRelevanceResult:
    if not getattr(settings, "OPENAI_API_KEY", ""):
        return _check_article_local_relevance(topic, article_data)

    try:
        payload = _ask_openai_about_relevance(topic, article_data)
        score = _normalize_score(payload.get("score"))
        is_ai_relevant = _normalize_bool(payload.get("is_relevant"))
        return AiRelevanceResult(
            is_relevant=is_ai_relevant and score >= 60,
            score=score,
            reason=_compact_relevance_reason(payload.get("reason") or "Новина відповідає темі."),
        )
    except Exception as error:
        # Якщо OpenAI тимчасово недоступний, використовуємо локальну
        # оцінку релевантності за темою та ключовими словами.
        logger.warning("AI relevance check failed, fallback used: %s", error)
        return _check_article_local_relevance(topic, article_data)


def _check_article_local_relevance(topic: MonitoringTopic, article_data) -> AiRelevanceResult:
    title = getattr(article_data, "title", "")
    content = getattr(article_data, "content", "")
    topic_terms = build_topic_terms(topic)

    if not topic_terms:
        return AiRelevanceResult(
            is_relevant=False,
            score=0,
            reason=_compact_relevance_reason("Немає термів теми для локальної перевірки."),
        )

    local_score = calculate_local_relevance_score(topic, title=title, content=content)
    is_relevant = local_score.score >= LOCAL_RELEVANCE_THRESHOLD
    return AiRelevanceResult(
        is_relevant=is_relevant,
        score=local_score.score,
        reason=_build_local_relevance_reason(
            is_relevant=is_relevant,
            matched_terms=list(local_score.matched_terms),
            title_matches=local_score.title_matches,
        ),
    )


def _ask_openai_about_relevance(topic: MonitoringTopic, article_data) -> dict:
    client = OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.OPENAI_TIMEOUT_SECONDS,
        max_retries=0,
    )
    response = client.responses.create(
        model=_resolve_openai_model(topic),
        temperature=0,
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Ти перевіряєш релевантність новини для системи моніторингу. "
                            "Поверни лише valid JSON без markdown і без будь-якого тексту поза JSON. "
                            "Не додавай коментарі, пояснення або службові примітки."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": _build_relevance_prompt(topic, article_data)}],
            },
        ],
    )
    return _extract_json_payload(response.output_text)


def _build_relevance_prompt(topic: MonitoringTopic, article_data) -> str:
    keywords = ", ".join(topic.keywords_list) or "не вказано"
    article_text = " ".join(
        part.strip()
        for part in [
            article_data.title or "",
            (article_data.content or "")[:2500],
        ]
        if part and part.strip()
    )

    return (
        "Оціни, чи новина справді відповідає темі моніторингу за змістом, "
        "а не просто випадково містить окреме слово.\n\n"
        "Поверни JSON такого формату:\n"
        '{"is_relevant": true, "score": 0, "reason": "..."}\n\n'
        "Правила:\n"
        "1. is_relevant має бути true лише якщо матеріал реально про задану тему.\n"
        "2. score від 0 до 100 показує силу відповідності.\n"
        "3. Якщо ключове слово згадане у випадковому контексті або в блоці схожих матеріалів, став false.\n"
        "4. Оцінюй головний фокус статті: заголовок, лід і основна подія мають відповідати темі користувача.\n"
        "5. Не розширюй тему самостійно до суміжних сфер. Якщо стаття лише побічно пов'язана з темою через організацію, країну, технологію, посаду, галузь або окреме слово, став false.\n"
        "6. Якщо тема користувача конкретна, а стаття описує ширший або сусідній контекст, став false або score нижче 60.\n"
        "7. Якщо стаття релевантна лише за другорядною деталлю, а головна подія про інше, став false.\n"
        "8. Якщо зв'язок із темою описаний як можливий майбутній наслідок, а не як поточна головна подія, став false.\n"
        "9. reason має бути дуже коротким українською мовою: одна фраза до 80 символів, без складних пояснень і без переносу рядка.\n\n"
        f"Тема моніторингу: {topic.topic}\n"
        f"Ключові слова користувача: {keywords}\n\n"
        f"Новина:\n{article_text}"
    )


def _extract_json_payload(response_text: str) -> dict:
    cleaned = (response_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("OpenAI не повернув JSON.")
    return json.loads(cleaned[start : end + 1])


def _normalize_score(value) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 0
    return max(0, min(score, 100))


def _normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _compact_relevance_reason(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return "Новина відповідає темі."
    text = text[:1].upper() + text[1:]
    if len(text) <= RELEVANCE_REASON_MAX_CHARS:
        return text

    shortened = text[:RELEVANCE_REASON_MAX_CHARS].rsplit(" ", 1)[0].strip(" ,.;:")
    return f"{shortened or text[:RELEVANCE_REASON_MAX_CHARS].strip()}..."


def _resolve_openai_model(topic: MonitoringTopic) -> str:
    if topic.owner:
        try:
            return topic.owner.newsroom_settings.openai_model
        except ObjectDoesNotExist:
            pass
    return getattr(settings, "OPENAI_MODEL", "gpt-4.1-mini")


def _build_local_relevance_reason(*, is_relevant: bool, matched_terms: list[str], title_matches: int) -> str:
    distinct_matches = len(matched_terms)
    if not distinct_matches:
        return _compact_relevance_reason("Локально не знайдено збігу з темою.")

    if is_relevant:
        if title_matches:
            return _compact_relevance_reason("Локальний збіг: знайдено ключові слова в заголовку.")
        return _compact_relevance_reason("Локальний збіг: знайдено ключові слова в тексті.")

    if distinct_matches == 1 and title_matches == 0:
        return _compact_relevance_reason("Локально знайдено лише один слабкий збіг у тексті.")

    return _compact_relevance_reason("Локальний збіг із темою виявився недостатнім.")
