from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from newsroom.models import (
    ActivityEvent,
    Article,
    Cluster,
    ContentTemplate,
    Draft,
    DraftLengthChoices,
    MonitoringTopic,
    Source,
    UserSettings,
)


def _update_or_create_first(model, lookup: dict, defaults: dict):
    instance = model.objects.filter(**lookup).first()
    if not instance:
        return model.objects.create(**lookup, **defaults)

    for field_name, value in defaults.items():
        setattr(instance, field_name, value)
    instance.save()
    return instance


class Command(BaseCommand):
    help = "Створює демонстраційні довідникові дані для дипломного проєкту."

    def handle(self, *args, **options):
        # Створюємо або оновлюємо демо-користувача,
        # під яким зручно показувати систему на захисті.
        demo_user = User.objects.filter(username="demo_editor").first()
        if not demo_user:
            demo_user = User.objects.create(
                username="demo_editor",
                email="editor@newsroom.local",
                first_name="Контент-редактор",
            )

        demo_user.email = "editor@newsroom.local"
        demo_user.first_name = "Контент-редактор"
        demo_user.set_password("demo12345")
        demo_user.save()

        # Залишаємо базові персональні налаштування для демо-акаунта.
        _update_or_create_first(
            UserSettings,
            {"user": demo_user},
            {
                "default_time_window": MonitoringTopic.TimeWindow.DAY,
                "default_draft_length": DraftLengthChoices.MEDIUM,
                "openai_model": "gpt-4.1-mini",
            },
        )

        # Очищаємо раніше створені демонстраційні робочі дані,
        # щоб після повторного seed у системі лишилися тільки довідники.
        ActivityEvent.objects.filter(owner=demo_user).delete()
        Draft.objects.filter(owner=demo_user).delete()
        Article.objects.filter(owner=demo_user).delete()
        Cluster.objects.filter(owner=demo_user).delete()

        # Шаблони контенту залишаємо в seed, бо це теж довідникові дані
        # і вони потрібні для демонстрації функціоналу генерації.
        templates_data = [
            {
                "name": "Новинний матеріал",
                "description": "Класичний шаблон для підготовки новини у нейтральному тоні.",
                "prompt_text": (
                    "Побудуй матеріал як новинний текст: короткий лід, основна частина, висновок. "
                    "Не вигадуй фактів і не використовуй зайву емоційність."
                ),
            },
            {
                "name": "Аналітичний розбір",
                "description": "Шаблон для пояснення причин, наслідків і значення новини.",
                "prompt_text": (
                    "Зроби матеріал більш аналітичним: покажи контекст, наслідки та ключові акценти теми. "
                    "Використай 2-3 підзаголовки."
                ),
            },
            {
                "name": "Контент для соцмереж",
                "description": "Короткий і динамічний формат для швидкої digital-публікації.",
                "prompt_text": (
                    "Підготуй компактний матеріал з виразним першим абзацом і стислим фінальним акцентом."
                ),
            },
        ]

        template_names: list[str] = []
        for template_data in templates_data:
            template_names.append(template_data["name"])
            _update_or_create_first(
                ContentTemplate,
                {"owner": demo_user, "name": template_data["name"]},
                {
                    "description": template_data["description"],
                    "prompt_text": template_data["prompt_text"],
                    "status": ContentTemplate.Status.ACTIVE,
                },
            )

        ContentTemplate.objects.filter(owner=demo_user).exclude(name__in=template_names).delete()

        # Створюємо лише ті джерела, які потрібні для демонстрації.
        sources_data = [
            {
                "name": "DEV.UA",
                "url": "https://dev.ua/",
                "type": Source.SourceType.SITE,
                "category": "technology",
            },
            {
                "name": "ITC",
                "url": "https://t.me/itcua",
                "type": Source.SourceType.TELEGRAM,
                "category": "technology",
            },
        ]

        sources_by_url: dict[str, Source] = {}
        source_urls: list[str] = []
        for source_data in sources_data:
            source_urls.append(source_data["url"])
            source = _update_or_create_first(
                Source,
                {"owner": demo_user, "url": source_data["url"]},
                {
                    "name": source_data["name"],
                    "type": source_data["type"],
                    "status": Source.Status.ACTIVE,
                    "category": source_data["category"],
                    "last_fetched_at": None,
                },
            )
            sources_by_url[source_data["url"]] = source

        Source.objects.filter(owner=demo_user).exclude(url__in=source_urls).delete()

        # Теми моніторингу теж формуємо як довідники:
        # без статей, кластерів і журналу подій.
        topics_data = [
            {
                "topic": "Штучний інтелект",
                "keywords": (
                    "штучний інтелект, AI, генеративний штучний інтелект, машинне навчання, "
                    "глибинне навчання, нейромережі, обробка природної мови, комп’ютерний зір, "
                    "автоматизація, генерація контенту, ChatGPT, LLM моделі, аналіз даних, big data, "
                    "інтелектуальні системи, AI стартапи, AI інструменти, цифрові помічники, "
                    "роботизація, технології штучного інтелекту, ШІ"
                ),
                "time_window": MonitoringTopic.TimeWindow.WEEK,
                "source_urls": [
                    "https://dev.ua/",
                    "https://t.me/itcua",
                ],
            },
            {
                "topic": "Бізнес та стартапи",
                "keywords": (
                    "бізнес, business, startup, стартап, entrepreneurship, компанії, corporate, "
                    "venture capital, VC, інвестиції, funding, бізнес-аналітика, IPO, acquisitions, "
                    "mergers, fintech, SaaS, e-commerce, B2B, B2C, малий бізнес, технологічні компанії, "
                    "бізнес-модель, бізнес-стратегія, підприємництво, market trends, бізнес-новини, "
                    "digital business, product launch, unicorn startup, founders, CEO, бізнес-розвиток, "
                    "remote work, productivity, economy, економіка, market analysis"
                ),
                "time_window": MonitoringTopic.TimeWindow.WEEK,
                "source_urls": [
                    "https://dev.ua/",
                    "https://t.me/itcua",
                ],
            },
        ]

        topic_names: list[str] = []
        for topic_data in topics_data:
            topic_names.append(topic_data["topic"])
            topic = _update_or_create_first(
                MonitoringTopic,
                {"owner": demo_user, "topic": topic_data["topic"]},
                {
                    "keywords": topic_data["keywords"],
                    "time_window": topic_data["time_window"],
                    "status": MonitoringTopic.Status.ACTIVE,
                    "run_state": MonitoringTopic.RunState.IDLE,
                    "cancel_requested": False,
                    "exact_date": None,
                    "date_from": None,
                    "date_to": None,
                    "run_started_at": None,
                    "last_run_at": None,
                },
            )
            topic.sources.set([sources_by_url[url] for url in topic_data["source_urls"]])

        MonitoringTopic.objects.filter(owner=demo_user).exclude(topic__in=topic_names).delete()

        self.stdout.write(
            self.style.SUCCESS(
                "Демонстраційні дані підготовлено.\n"
                "Дані для входу:\n"
                "логін: demo_editor\n"
                "пароль: demo12345"
            )
        )
