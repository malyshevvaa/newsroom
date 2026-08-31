from __future__ import annotations

import logging
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from openai import OpenAI

from ..models import ActivityEvent, Article, ContentTemplate, Draft
from .text_tools import strip_html

logger = logging.getLogger(__name__)

LENGTH_GUIDES = {
    "short": "180-250 слів",
    "medium": "350-550 слів",
    "long": "700-1000 слів",
}
MAX_PROMPT_ARTICLE_LENGTH = 12000
MAX_PROMPT_EXAMPLE_LENGTH = 2000
MAX_PROMPT_EXAMPLES = 3


class DraftGenerationError(Exception):
    """Помилка генерації чернетки, коли AI-сервіс недоступний або не повернув текст."""


def build_default_title_from_article(article: Article) -> str:
    # Якщо користувач не ввів власну назву, як робочу назву чернетки
    # просто беремо заголовок вихідної новини.
    article_title = _normalize_title_text(article.title)
    if article_title:
        return _fit_title(article_title)
    return "Новий матеріал за вибраною статтею"


def generate_draft_from_article(
    article: Article,
    *,
    content_template: ContentTemplate,
    target_length: str,
    additional_instructions: str,
    custom_title: str,
    current_user: User,
) -> Draft:
    if not current_user:
        raise DraftGenerationError("Не вдалося визначити користувача для генерації матеріалу.")

    resolved_title = _resolve_draft_title(article, custom_title)
    model_name = _resolve_openai_model(current_user)
    content = _generate_text_with_openai(
        article=article,
        content_template=content_template,
        target_length=target_length,
        additional_instructions=additional_instructions,
        title=resolved_title,
        model_name=model_name,
    )

    draft = Draft.objects.create(
        owner=current_user,
        title=resolved_title,
        content=content,
        source_article=article,
        content_template=content_template,
        target_length=target_length,
        generated_at=timezone.now(),
    )

    ActivityEvent.log(
        owner=current_user,
        description=f"Згенеровано контент за статтею: {article.title}",
    )
    return draft


def _resolve_draft_title(article: Article, custom_title: str) -> str:
    clean_custom_title = _normalize_title_text(custom_title)
    if clean_custom_title:
        return _fit_title(clean_custom_title)
    return build_default_title_from_article(article)


def _normalize_title_text(value: str) -> str:
    text = " ".join(strip_html(value or "").split())
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()


def _fit_title(value: str) -> str:
    text = _normalize_title_text(value)
    if len(text) <= 255:
        return text
    return text[:255].rsplit(" ", 1)[0].strip()


def _resolve_openai_model(current_user: User) -> str:
    try:
        return current_user.newsroom_settings.openai_model
    except ObjectDoesNotExist:
        pass
    return getattr(settings, "OPENAI_MODEL", "gpt-4.1-mini")


def _generate_text_with_openai(
    *,
    article: Article,
    content_template: ContentTemplate,
    target_length: str,
    additional_instructions: str,
    title: str,
    model_name: str,
) -> str:
    prompt = _build_prompt(
        article=article,
        content_template=content_template,
        target_length=target_length,
        additional_instructions=additional_instructions,
        title=title,
    )
    if not settings.OPENAI_API_KEY:
        raise DraftGenerationError("Для генерації контенту потрібно налаштувати OpenAI API.")

    try:
        client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=0,
        )
        response = client.responses.create(
            model=model_name,
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Ти допомагаєш редактору готувати якісний контент українською мовою. "
                                "Не вигадуй фактів, спирайся лише на надану статтю та інструкції. "
                                "Поверни тільки готовий текст матеріалу без службових приміток."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            ],
        )
    except Exception as exc:
        # Генерація є AI-функцією, тому без відповіді OpenAI
        # не підміняємо результат локальною заготовкою.
        logger.exception(
            "Не вдалося згенерувати чернетку через OpenAI.",
            extra={
                "article_id": article.id,
                "content_template_id": content_template.id,
                "model_name": model_name,
            },
        )
        raise DraftGenerationError("Не вдалося отримати відповідь від OpenAI для генерації контенту.") from exc

    generated_text = (response.output_text or "").strip()
    if generated_text:
        return generated_text

    raise DraftGenerationError("OpenAI не повернув текст для згенерованого матеріалу.")


def _build_prompt(
    *,
    article: Article,
    content_template: ContentTemplate,
    target_length: str,
    additional_instructions: str,
    title: str,
) -> str:
    article_text = _prepare_article_text_for_prompt(article)
    monitoring_topic = article.monitoring_topic.topic if article.monitoring_topic else "не вказано"
    source_name = article.source.name if article.source_id else "не вказано"
    example_texts = _format_template_examples_for_prompt(content_template)
    examples_section = ""
    if example_texts:
        # Приклади можуть бути довгими, тому обмежуємо їх у промпті.
        # Вони потрібні лише як стилістичний орієнтир, а не як джерело фактів.
        examples_section = (
            "Приклади написаних текстів для цього шаблону:\n"
            f"{example_texts}\n\n"
        )

    return (
        "Підготуй готовий матеріал українською мовою за правилами вибраного шаблону.\n"
        f"Робоча назва матеріалу: {title}\n"
        f"Рекомендована довжина: {LENGTH_GUIDES.get(target_length, '350-550 слів')}.\n"
        f"Шаблон: {content_template.name}\n"
        f"Опис шаблону: {content_template.description or 'без додаткового опису'}\n"
        f"Інструкція шаблону: {content_template.prompt_text}\n\n"
        f"{examples_section}"
        f"Тема моніторингу: {monitoring_topic}\n"
        f"Джерело: {source_name}\n"
        f"Оригінальний заголовок: {article.title}\n"
        "\n"
        "Вимоги до результату:\n"
        "1. Не дублюй назву як markdown-заголовок першого рівня.\n"
        "2. Побудуй зв'язний готовий матеріал для публікації.\n"
        "3. За потреби використай 2-3 підзаголовки.\n"
        "4. Не використовуй списки, якщо вони не критично потрібні.\n"
        "5. Якщо наведені приклади текстів, використовуй їх тільки як орієнтир для структури, ритму й тону.\n"
        "6. Не копіюй з прикладів факти, речення, персонажів або формулювання.\n"
        f"7. Додаткові вказівки: {additional_instructions or 'без додаткових вказівок'}\n\n"
        f"Текст статті-основи:\n{article_text}"
    )


def _prepare_article_text_for_prompt(article: Article) -> str:
    # Перед відправленням у prompt прибираємо HTML і обмежуємо розмір,
    # щоб генерація не ламалась на дуже довгих текстах.
    clean_text = " ".join(strip_html(article.content or article.title).split())
    if len(clean_text) <= MAX_PROMPT_ARTICLE_LENGTH:
        return clean_text

    shortened_text = clean_text[:MAX_PROMPT_ARTICLE_LENGTH].rsplit(" ", 1)[0].strip()
    return f"{shortened_text}..."


def _format_template_examples_for_prompt(content_template: ContentTemplate) -> str:
    # Приклади з шаблону передаємо в промпт як короткі текстові блоки.
    # Так ШІ бачить стиль і структуру, але промпт не роздувається зайвим текстом.
    raw_examples = content_template.example_texts or []
    if not isinstance(raw_examples, list):
        return ""

    formatted_examples: list[str] = []
    for index, example in enumerate(raw_examples[:MAX_PROMPT_EXAMPLES], start=1):
        text = str(example).strip()
        if not text:
            continue

        shortened_text = text[:MAX_PROMPT_EXAMPLE_LENGTH].strip()
        if len(text) > MAX_PROMPT_EXAMPLE_LENGTH:
            shortened_text = f"{shortened_text}..."

        formatted_examples.append(f"Приклад {index}:\n{shortened_text}")

    return "\n\n".join(formatted_examples)
