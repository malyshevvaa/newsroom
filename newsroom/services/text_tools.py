from __future__ import annotations

import re

from bs4 import BeautifulSoup


def strip_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def normalize_text(value: str) -> str:
    # Для локальних перевірок і хешування приводимо текст до простого
    # нормалізованого вигляду без HTML, зайвої пунктуації та дубльованих пробілів.
    text = strip_html(value or "").lower()
    text = re.sub(r"[^\w\s'\-]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def count_words(text: str) -> int:
    return len((text or "").split())
