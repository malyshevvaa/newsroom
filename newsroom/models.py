from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def default_openai_model() -> str:
    return getattr(settings, "OPENAI_MODEL", "gpt-4.1-mini")


class DraftLengthChoices(models.TextChoices):
    SHORT = "short", "Коротка"
    MEDIUM = "medium", "Середня"
    LONG = "long", "Довга"


class Source(models.Model):
    class SourceType(models.TextChoices):
        SITE = "site", "Сайт"
        TELEGRAM = "telegram", "Telegram"

    class Status(models.TextChoices):
        ACTIVE = "active", "Активне"
        ARCHIVED = "archived", "Архів"

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_sources",
        verbose_name="Власник",
    )
    name = models.CharField(max_length=150, verbose_name="Назва")
    url = models.URLField(max_length=500, verbose_name="URL")
    type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.SITE, verbose_name="Тип")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, verbose_name="Статус")
    category = models.CharField(max_length=100, blank=True, verbose_name="Категорія")
    last_fetched_at = models.DateTimeField(null=True, blank=True, verbose_name="Останній запуск моніторингу")

    class Meta:
        ordering = ["name"]
        verbose_name = "Джерело"
        verbose_name_plural = "Джерела"
        constraints = [
            models.UniqueConstraint(fields=["owner", "url"], name="unique_source_url_per_owner"),
        ]

    def __str__(self) -> str:
        return self.name

class MonitoringTopic(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активна"
        ARCHIVED = "archived", "Архів"

    class RunState(models.TextChoices):
        IDLE = "idle", "Готова"
        RUNNING = "running", "Виконується"
        ERROR = "error", "Помилка"

    class TimeWindow(models.TextChoices):
        DAY = "24h", "Остання доба"
        WEEK = "7d", "Останній тиждень"
        MONTH = "30d", "Останній місяць"
        EXACT_DATE = "date", "Конкретна дата"
        DATE_RANGE = "range", "Період за датами"

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_topics",
        verbose_name="Власник",
    )
    topic = models.CharField(max_length=255, verbose_name="Тема моніторингу")
    keywords = models.TextField(verbose_name="Ключові слова та фрази")
    time_window = models.CharField(
        max_length=8,
        choices=TimeWindow.choices,
        default=TimeWindow.DAY,
        verbose_name="Часовий фільтр",
    )
    exact_date = models.DateField(null=True, blank=True, verbose_name="Конкретна дата")
    date_from = models.DateField(null=True, blank=True, verbose_name="Дата від")
    date_to = models.DateField(null=True, blank=True, verbose_name="Дата до")
    sources = models.ManyToManyField(Source, blank=True, related_name="monitoring_topics", verbose_name="Джерела")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, verbose_name="Статус")
    run_state = models.CharField(
        max_length=16,
        choices=RunState.choices,
        default=RunState.IDLE,
        verbose_name="Стан запуску",
    )
    run_started_at = models.DateTimeField(null=True, blank=True, verbose_name="Початок запуску")
    # Прапорець потрібен для м'якої зупинки фонового збору.
    # Потік не вбивається примусово, а сам завершується після найближчої перевірки.
    cancel_requested = models.BooleanField(default=False, verbose_name="Запит на зупинку")
    last_run_at = models.DateTimeField(null=True, blank=True, verbose_name="Останній запуск")

    class Meta:
        ordering = ["topic"]
        verbose_name = "Тема моніторингу"
        verbose_name_plural = "Теми моніторингу"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "topic"],
                name="unique_monitoring_topic_per_owner",
            ),
        ]

    def __str__(self) -> str:
        return self.topic

    def clean(self) -> None:
        errors = {}

        if self.time_window == self.TimeWindow.EXACT_DATE:
            if not self.exact_date:
                errors["exact_date"] = "Для режиму «Конкретна дата» потрібно вказати дату."
            if self.date_from:
                errors["date_from"] = "Поле «Дата від» використовується лише для режиму «Період за датами»."
            if self.date_to:
                errors["date_to"] = "Поле «Дата до» використовується лише для режиму «Період за датами»."

        elif self.time_window == self.TimeWindow.DATE_RANGE:
            if not self.date_from:
                errors["date_from"] = "Для режиму «Період за датами» потрібно вказати дату початку."
            if not self.date_to:
                errors["date_to"] = "Для режиму «Період за датами» потрібно вказати дату завершення."
            if self.exact_date:
                errors["exact_date"] = "Поле «Конкретна дата» використовується лише для відповідного режиму."
            if self.date_from and self.date_to and self.date_from > self.date_to:
                errors["date_to"] = "Дата завершення не може бути раніше дати початку."

        else:
            if self.exact_date:
                errors["exact_date"] = "Конкретну дату можна вказувати лише для відповідного режиму."
            if self.date_from:
                errors["date_from"] = "Діапазон дат можна вказувати лише для режиму «Період за датами»."
            if self.date_to:
                errors["date_to"] = "Діапазон дат можна вказувати лише для режиму «Період за датами»."

        # Джерела теми мають належати тому самому користувачу, що і сама тема.
        if self.owner_id and self.pk:
            foreign_sources = self.sources.exclude(owner_id=self.owner_id)
            if foreign_sources.exists():
                errors["sources"] = "До теми моніторингу можна прив’язувати лише власні джерела."

        if errors:
            raise ValidationError(errors)

    @property
    def keywords_list(self) -> list[str]:
        normalized = (self.keywords or "").replace("\n", ",").replace(";", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def time_window_start(self) -> datetime:
        windows = {
            self.TimeWindow.DAY: timedelta(days=1),
            self.TimeWindow.WEEK: timedelta(days=7),
            self.TimeWindow.MONTH: timedelta(days=30),
        }
        return timezone.now() - windows.get(self.time_window, timedelta(days=1))

    @property
    def time_filter_label(self) -> str:
        if self.time_window == self.TimeWindow.EXACT_DATE and self.exact_date:
            return self.exact_date.strftime("%d.%m.%Y")
        if self.time_window == self.TimeWindow.DATE_RANGE and self.date_from and self.date_to:
            return f"{self.date_from.strftime('%d.%m.%Y')} - {self.date_to.strftime('%d.%m.%Y')}"
        return self.get_time_window_display()

    @property
    def is_running(self) -> bool:
        return self.run_state == self.RunState.RUNNING

    @property
    def current_articles_count(self) -> int:
        # Для інтерфейсу беремо актуальний Count із запиту, якщо він є.
        # Якщо тему використовують без анотації, рахуємо матеріали через зв'язок зі статтями.
        return getattr(self, "live_articles_count", self.articles.count())

    def matches_published_at(self, published_at) -> bool:
        if not published_at:
            return True

        # Для точної дати й періоду порівнюємо саме календарну дату, щоб час публікації не змінював результат відбору.
        published_date = timezone.localtime(published_at).date() if timezone.is_aware(published_at) else published_at.date()

        if self.time_window == self.TimeWindow.EXACT_DATE and self.exact_date:
            return published_date == self.exact_date

        if self.time_window == self.TimeWindow.DATE_RANGE and self.date_from and self.date_to:
            return self.date_from <= published_date <= self.date_to

        return published_at >= self.time_window_start()


class Cluster(models.Model):
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_clusters",
        verbose_name="Власник",
    )
    monitoring_topic = models.ForeignKey(
        MonitoringTopic,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clusters",
        verbose_name="Тема моніторингу",
    )
    title = models.CharField(max_length=255, verbose_name="Назва кластера")
    summary = models.TextField(blank=True, verbose_name="Короткий опис")
    last_updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        ordering = ["-last_updated_at"]
        verbose_name = "Кластер"
        verbose_name_plural = "Кластери"

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        errors = {}

        if self.monitoring_topic and self.owner_id and self.monitoring_topic.owner_id != self.owner_id:
            errors["monitoring_topic"] = "Кластер можна пов’язати лише з темою моніторингу того самого користувача."

        if errors:
            raise ValidationError(errors)

    @property
    def current_article_count(self) -> int:
        # Кількість новин рахуємо динамічно через пов'язані записи, щоб не дублювати обчислюване значення окремим полем у БД.
        return getattr(self, "live_article_count", self.articles.filter(owner=self.owner).count())


class Article(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Нове"
        PROCESSED = "processed", "Оброблене"
        CLUSTERED = "clustered", "У кластері"
        REJECTED = "rejected", "Відхилене"

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_articles",
        verbose_name="Власник",
    )
    source = models.ForeignKey(
        Source,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="articles",
        verbose_name="Джерело",
    )
    monitoring_topic = models.ForeignKey(
        MonitoringTopic,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="articles",
        verbose_name="Тема моніторингу",
    )
    cluster = models.ForeignKey(
        Cluster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="articles",
        verbose_name="Кластер",
    )
    title = models.CharField(max_length=255, verbose_name="Заголовок")
    url = models.URLField(max_length=500, verbose_name="URL матеріалу")
    normalized_url = models.URLField(max_length=500, blank=True, db_index=True, verbose_name="Нормалізований URL")
    content_hash = models.CharField(max_length=64, blank=True, db_index=True, verbose_name="Хеш змісту")
    content = models.TextField(blank=True, verbose_name="Повний текст")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW, verbose_name="Статус")
    published_at = models.DateTimeField(null=True, blank=True, verbose_name="Опубліковано")
    fetched_at = models.DateTimeField(default=timezone.now, verbose_name="Отримано")
    # Зберігаємо підсумкову оцінку релевантності незалежно від того, чи вона була уточнена зовнішнім AI-сервісом, чи визначена локально.
    relevance_score = models.PositiveSmallIntegerField(default=0, verbose_name="Оцінка релевантності")
    relevance_reason = models.CharField(max_length=255, blank=True, verbose_name="Пояснення релевантності")
    # Якщо редактор вручну погодив статтю після відхилення ШІ, система не повинна повторно прибирати її з робочого списку.
    manual_relevance_approved = models.BooleanField(
        default=False,
        verbose_name="Схвалено редактором після ШІ",
    )
    is_favorite = models.BooleanField(default=False, verbose_name="В обраному")

    class Meta:
        ordering = ["-fetched_at"]
        verbose_name = "Стаття"
        verbose_name_plural = "Статті"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "monitoring_topic", "url"],
                name="unique_article_url_per_topic_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(relevance_score__gte=0, relevance_score__lte=100),
                name="article_relevance_score_range",
            ),
        ]

    def clean(self) -> None:
        errors = {}

        if self.source and self.owner_id and self.source.owner_id != self.owner_id:
            errors["source"] = "Статтю можна пов’язати лише з джерелом того самого користувача."

        if self.monitoring_topic and self.owner_id and self.monitoring_topic.owner_id != self.owner_id:
            errors["monitoring_topic"] = "Статтю можна пов’язати лише з темою моніторингу того самого користувача."

        if self.cluster and self.owner_id and self.cluster.owner_id != self.owner_id:
            errors["cluster"] = "Статтю можна пов’язати лише з кластером того самого користувача."

        # Якщо стаття вже має і тему, і кластер, вони повинні узгоджуватися між собою.
        if self.cluster and self.monitoring_topic and self.cluster.monitoring_topic_id:
            if self.cluster.monitoring_topic_id != self.monitoring_topic_id:
                errors["cluster"] = "Кластер статті має належати тій самій темі моніторингу."

        if errors:
            raise ValidationError(errors)

    @property
    def preview_text(self) -> str:
        from .services.text_tools import strip_html

        content_text = " ".join(strip_html(self.content or "").split())
        if content_text:
            return content_text
        return self.title or ""

    def __str__(self) -> str:
        return self.title


class ContentTemplate(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активний"
        ARCHIVED = "archived", "Архів"

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_content_templates",
        verbose_name="Власник",
    )
    name = models.CharField(max_length=120, verbose_name="Назва шаблону")
    description = models.TextField(blank=True, verbose_name="Короткий опис")
    prompt_text = models.TextField(verbose_name="Інструкція для генерації")
    example_texts = models.JSONField(default=list, blank=True, verbose_name="Приклади текстів")
    # Шаблон не видаляємо одразу з робочого процесу, а переносимо в архів.
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, verbose_name="Статус")

    class Meta:
        ordering = ["name"]
        verbose_name = "Шаблон контенту"
        verbose_name_plural = "Шаблони контенту"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"],
                name="unique_content_template_name_per_owner",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Draft(models.Model):
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_drafts",
        verbose_name="Власник",
    )
    title = models.CharField(max_length=255, verbose_name="Назва")
    content = models.TextField(blank=True, verbose_name="Текст")
    # Зберігаємо статтю і шаблон окремо, щоб розуміти, з якого джерела та за якими правилами отримано фінальний матеріал.
    source_article = models.ForeignKey(
        Article,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_drafts",
        verbose_name="Вихідна стаття",
    )
    content_template = models.ForeignKey(
        ContentTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drafts",
        verbose_name="Шаблон контенту",
    )
    # Додаткові вказівки використовуємо лише в момент генерації.
    # У самій чернетці зберігаємо вже готовий результат, а не технічні параметри prompt.
    target_length = models.CharField(
        max_length=16,
        choices=DraftLengthChoices.choices,
        default=DraftLengthChoices.MEDIUM,
        verbose_name="Цільова довжина",
    )
    # Чернетка в системі є саме результатом генерації, тому час її створення завжди фіксуємо і не дозволяємо порожнє значення.
    generated_at = models.DateTimeField(default=timezone.now, verbose_name="Згенеровано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Чернетка"
        verbose_name_plural = "Чернетки"

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        errors = {}

        if self.source_article and self.owner_id and self.source_article.owner_id != self.owner_id:
            errors["source_article"] = "Чернетку можна пов’язати лише зі статтею того самого користувача."

        if self.content_template and self.owner_id and self.content_template.owner_id != self.owner_id:
            errors["content_template"] = "Чернетку можна пов’язати лише з шаблоном того самого користувача."

        if errors:
            raise ValidationError(errors)

    @property
    def word_count(self) -> int:
        # Кількість слів не зберігаємо окремо в БД, бо вона повністю обчислюється з тексту чернетки.
        from .services.text_tools import count_words

        return count_words(self.content or "")


class UserSettings(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_settings",
        verbose_name="Користувач",
    )
    default_time_window = models.CharField(
        max_length=8,
        choices=MonitoringTopic.TimeWindow.choices,
        default=MonitoringTopic.TimeWindow.DAY,
        verbose_name="Типовий часовий фільтр",
    )
    default_draft_length = models.CharField(
        max_length=16,
        choices=DraftLengthChoices.choices,
        default=DraftLengthChoices.MEDIUM,
        verbose_name="Типова довжина чернетки",
    )
    openai_model = models.CharField(
        max_length=100,
        default=default_openai_model,
        verbose_name="Модель OpenAI",
    )

    class Meta:
        verbose_name = "Налаштування користувача"
        verbose_name_plural = "Налаштування користувачів"

    def __str__(self) -> str:
        return f"Налаштування {self.user.username}"


class ActivityEvent(models.Model):
    MAX_EVENTS_PER_OWNER = 20

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="newsroom_activity_events",
        verbose_name="Власник",
    )
    description = models.CharField(max_length=255, verbose_name="Опис")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Створено")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Подія активності"
        verbose_name_plural = "Події активності"

    def __str__(self) -> str:
        return self.description

    @classmethod
    def log(cls, *, owner, description: str, created_at=None):
        # Подія активності працює як короткий журнал повідомлень, тому зберігаємо лише текст події, власника та час створення.
        return cls.objects.create(
            owner=owner,
            description=description,
            created_at=created_at or timezone.now(),
        )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if not self.owner_id:
            return

        # Зберігаємо лише коротку актуальну історію подій для кожного користувача, щоб журнал активності не ріс без потреби і залишався придатним для інтерфейсу.
        stale_event_ids = list(
            ActivityEvent.objects.filter(owner_id=self.owner_id)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)[self.MAX_EVENTS_PER_OWNER :]
        )
        if stale_event_ids:
            ActivityEvent.objects.filter(id__in=stale_event_ids).delete()


