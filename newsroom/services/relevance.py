from __future__ import annotations

from dataclasses import dataclass
import re

from ..models import MonitoringTopic
from .text_tools import normalize_text

# Бали для локального fallback, якщо OpenAI недоступний.
LOCAL_RELEVANCE_THRESHOLD = 45
TITLE_TERM_SCORE = 30
CONTENT_TERM_SCORE = 15


@dataclass(frozen=True)
class LocalRelevanceScore:
    score: int
    matched_terms: tuple[str, ...]
    title_matches: int
    content_matches: int

    @property
    def distinct_matches(self) -> int:
        return len(self.matched_terms)


def build_topic_terms(topic: MonitoringTopic) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    # Для локальної перевірки беремо лише явні дані користувача:
    # назву теми та її ключові слова без автоматичного розширення словника.
    for raw_value in [topic.topic, *topic.keywords_list]:
        normalized = normalize_text(raw_value)
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)

    return terms


def matches_monitoring_topic(topic: MonitoringTopic, *, title: str, content: str) -> bool:
    title_matches, content_matches = _collect_term_occurrence_counts(
        topic,
        title=title,
        content=content,
    )

    # Для первинного відбору використовуємо просте правило:
    # один збіг у заголовку або щонайменше два збіги в тексті.
    return title_matches >= 1 or content_matches >= 2


def calculate_local_relevance_score(topic: MonitoringTopic, *, title: str, content: str) -> LocalRelevanceScore:
    matched_terms, title_matches, content_matches = _collect_term_match_counts(
        topic,
        title=title,
        content=content,
    )
    raw_score = title_matches * TITLE_TERM_SCORE + content_matches * CONTENT_TERM_SCORE

    # Цей score використовується як fallback, якщо AI-перевірка недоступна.
    score = raw_score

    # Кілька різних термінів теми означають сильніший збіг,
    # ніж багаторазове повторення одного й того самого слова.
    distinct_matches = len(matched_terms)
    if distinct_matches >= 4:
        score += 30
    elif distinct_matches == 3:
        score += 20
    elif distinct_matches == 2:
        score += 10

    # Один слабкий збіг тільки в тексті без підтримки заголовком
    # не вважаємо достатнім сигналом релевантності.
    if distinct_matches == 1 and title_matches == 0:
        score -= 15
    if title_matches == 0:
        score -= 5

    return LocalRelevanceScore(
        score=max(0, min(score, 100)),
        matched_terms=matched_terms,
        title_matches=title_matches,
        content_matches=content_matches,
    )


def _collect_term_match_counts(
    topic: MonitoringTopic,
    *,
    title: str,
    content: str,
) -> tuple[tuple[str, ...], int, int]:
    title_text = normalize_text(title)
    content_text = normalize_text(content)
    topic_terms = build_topic_terms(topic)

    # Рахуємо лише факт наявності терма в заголовку і тексті,
    # а не кількість повторень одного й того самого слова.
    matched_terms: list[str] = []
    title_matches = 0
    content_matches = 0

    for term in topic_terms:
        in_title = _contains_term(title_text, term)
        in_content = _contains_term(content_text, term)
        if not in_title and not in_content:
            continue

        matched_terms.append(term)
        if in_title:
            title_matches += 1
        if in_content:
            content_matches += 1

    return tuple(matched_terms), title_matches, content_matches


def _collect_term_occurrence_counts(
    topic: MonitoringTopic,
    *,
    title: str,
    content: str,
) -> tuple[int, int]:
    title_text = normalize_text(title)
    content_text = normalize_text(content)
    topic_terms = build_topic_terms(topic)

    title_matches = 0
    content_matches = 0

    for term in topic_terms:
        title_matches += _count_term_occurrences(title_text, term)
        content_matches += _count_term_occurrences(content_text, term)

    return title_matches, content_matches


def _contains_term(text: str, term: str) -> bool:
    if not text or not term:
        return False
    # Перевіряємо збіг по межах слова, щоб не ловити випадкові частини інших слів.
    pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _count_term_occurrences(text: str, term: str) -> int:
    if not text or not term:
        return 0
    pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))
