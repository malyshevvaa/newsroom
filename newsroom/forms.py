from urllib.parse import urlparse, urlunparse

from django import forms
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.forms import PasswordChangeForm, UserCreationForm
from django.contrib.auth.models import User
from django.utils import timezone

from .models import Article, ContentTemplate, Draft, DraftLengthChoices, MonitoringTopic, Source, UserSettings


class StyledFormMixin:
    # Один раз задаємо CSS-класи для полів, щоб шаблони залишалися чистими.
    def apply_styles(self):
        for field in self.fields.values():
            widget = field.widget
            base_class = "form-control"
            if isinstance(widget, forms.Textarea):
                base_class = "form-textarea"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                base_class = "form-select"
            elif isinstance(widget, forms.CheckboxInput):
                base_class = "form-checkbox"

            current = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{current} {base_class}".strip()


class RegistrationForm(UserCreationForm, StyledFormMixin):
    display_name = forms.CharField(label="Ім'я редактора", max_length=120)
    email = forms.EmailField(label="Email")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("display_name", "username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Логін"
        self.fields["password1"].label = "Пароль"
        self.fields["password2"].label = "Підтвердження пароля"
        self.apply_styles()

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Користувач із таким email вже існує.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        display_name = self.cleaned_data["display_name"].strip()
        user.email = self.cleaned_data["email"]
        user.first_name = display_name
        if commit:
            user.save()
            UserSettings.objects.get_or_create(user=user)
        return user


class LoginForm(StyledFormMixin, forms.Form):
    username = forms.CharField(label="Логін")
    password = forms.CharField(label="Пароль", widget=forms.PasswordInput())

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.user = None
        self.apply_styles()

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get("username", "").strip()
        password = cleaned_data.get("password", "")
        if not username or not password:
            return cleaned_data

        self.user = authenticate(self.request, username=username, password=password)
        if not self.user:
            raise forms.ValidationError("Невірний логін або пароль.")
        return cleaned_data


class UserPasswordChangeForm(PasswordChangeForm, StyledFormMixin):
    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["old_password"].label = "Поточний пароль"
        self.fields["new_password1"].label = "Новий пароль"
        self.fields["new_password2"].label = "Підтвердження нового пароля"
        self.apply_styles()


class ProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "email"]
        labels = {
            "first_name": "Ім'я редактора",
            "email": "Email",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Профільна форма має використовувати ті самі стилі,
        # що й інші форми розділу налаштувань.
        self.apply_styles()

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        duplicate = User.objects.filter(email__iexact=email).exclude(id=self.instance.id)
        if duplicate.exists():
            raise forms.ValidationError("Цей email вже використовується в іншому акаунті.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save(update_fields=["first_name", "email"])
        return user


class UserSettingsForm(StyledFormMixin, forms.ModelForm):
    openai_model = forms.ChoiceField(label="Модель OpenAI")

    class Meta:
        model = UserSettings
        fields = [
            "default_time_window",
            "default_draft_length",
            "openai_model",
        ]
        labels = {
            "default_time_window": "Типовий часовий фільтр",
            "default_draft_length": "Типова довжина чернетки",
            "openai_model": "Модель OpenAI",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_time_window"].choices = [
            choice
            for choice in MonitoringTopic.TimeWindow.choices
            if choice[0] in {
                MonitoringTopic.TimeWindow.DAY,
                MonitoringTopic.TimeWindow.WEEK,
                MonitoringTopic.TimeWindow.MONTH,
            }
        ]
        configured_model = self.instance.openai_model or getattr(settings, "OPENAI_MODEL", "gpt-4.1-mini")
        available_models = list(getattr(settings, "OPENAI_MODEL_OPTIONS", []))
        if configured_model not in available_models:
            available_models.insert(0, configured_model)

        # Модель OpenAI обираємо зі списку, щоб користувач не вводив
        # довільне значення, яке потім зламає генерацію або оцінку релевантності.
        self.fields["openai_model"] = forms.ChoiceField(
            label="Модель OpenAI",
            choices=[
            (model_name, model_name)
            for model_name in available_models
            ],
            initial=configured_model,
            help_text=(
            "Оберіть одну з доступних моделей OpenAI."
            ),
        )
        self.apply_styles()


class SourceForm(StyledFormMixin, forms.ModelForm):
    # URL вводимо як звичайний текст, щоб браузер не блокував адреси
    # без схеми на кшталт ukr.net ще до нашої серверної нормалізації.
    url = forms.CharField(label="URL")

    class Meta:
        model = Source
        fields = ["name", "url", "type", "category", "status"]

    def __init__(self, *args, **kwargs):
        self.owner = kwargs.pop("owner", None)
        super().__init__(*args, **kwargs)
        self.fields["url"].widget = forms.TextInput(
            attrs={
                "inputmode": "url",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        )
        self.fields["url"].help_text = "Для сайту вставте звичайний URL ресурсу. Для Telegram вставте публічне посилання на канал."
        # У ручному редагуванні залишаємо тільки бізнес-стани джерела.
        # Технічні збої збору зберігаються окремо в діагностиці останнього запуску.
        self.fields["status"].choices = [
            (Source.Status.ACTIVE, Source.Status.ACTIVE.label),
            (Source.Status.ARCHIVED, Source.Status.ARCHIVED.label),
        ]
        self.selected_status = self["status"].value() or Source.Status.ACTIVE
        self.apply_styles()

    def clean_url(self):
        url = (self.cleaned_data.get("url") or "").strip()
        if url and "://" not in url:
            url = f"https://{url}"

        # На цьому етапі поле type ще може бути не очищене,
        # тому тут виконуємо лише базову нормалізацію та загальну перевірку URL.
        forms.URLField().clean(url)
        return url

    def clean(self):
        cleaned_data = super().clean()
        url = cleaned_data.get("url")
        source_type = cleaned_data.get("type") or Source.SourceType.SITE

        if not url:
            return cleaned_data

        parsed = urlparse(url)
        hostname = (parsed.netloc or "").lower().replace("www.", "")
        is_telegram_host = hostname in {"t.me", "telegram.me"}

        # Якщо користувач вставив Telegram-посилання, але залишив тип "Сайт",
        # не даємо зберегти таке джерело в неправильній категорії.
        if is_telegram_host and source_type != Source.SourceType.TELEGRAM:
            self.add_error("type", "Для Telegram-посилань потрібно обрати тип Telegram.")
            return cleaned_data

        if source_type == Source.SourceType.TELEGRAM:
            url = self._normalize_telegram_url(url)

        if self.owner and Source.objects.filter(owner=self.owner, url=url).exclude(pk=self.instance.pk).exists():
            self.add_error("url", "Таке джерело вже є у вашому акаунті.")
            return cleaned_data

        cleaned_data["url"] = url
        return cleaned_data

    @staticmethod
    def _normalize_telegram_url(url: str) -> str:
        parsed = urlparse(url)
        hostname = (parsed.netloc or "").lower().replace("www.", "")
        if hostname not in {"t.me", "telegram.me"}:
            raise forms.ValidationError("Для типу Telegram потрібно вказати публічне посилання виду https://t.me/channel_name.")

        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            raise forms.ValidationError("Посилання на Telegram-канал має містити назву каналу.")

        if path_parts[0] == "s" and len(path_parts) > 1:
            channel_slug = path_parts[1]
        else:
            channel_slug = path_parts[0]

        if channel_slug in {"share", "joinchat", "addstickers"}:
            raise forms.ValidationError("Потрібно вказати саме публічний канал, а не сервісне Telegram-посилання.")

        normalized_path = f"/{channel_slug}/"
        return urlunparse(("https", "t.me", normalized_path, "", "", ""))


class MonitoringTopicForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = MonitoringTopic
        fields = [
            "topic",
            "keywords",
            "time_window",
            "exact_date",
            "date_from",
            "date_to",
            "sources",
            "status",
        ]
        widgets = {
            "keywords": forms.Textarea(
                attrs={
                    "class": "keywords-input",
                    "rows": 2,
                    "placeholder": "Наприклад: штучний інтелект, generative AI, OpenAI",
                }
            ),
            # HTML input[type="date"] приймає значення тільки у форматі YYYY-MM-DD.
            # Якщо Django віддає локальний формат на кшталт 08.04.2026,
            # браузер показує поле порожнім під час редагування.
            "exact_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "date_from": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "date_to": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "sources": forms.SelectMultiple(attrs={"size": 4}),
        }

    def __init__(self, *args, **kwargs):
        self.owner = kwargs.pop("owner", None)
        user_settings = kwargs.pop("user_settings", None)
        super().__init__(*args, **kwargs)
        today_iso = timezone.localdate().isoformat()
        queryset = Source.objects.none()
        if self.owner:
            # У темі можна обрати лише активні джерела: архівні джерела тимчасово
            # виключаються з моніторингу без видалення з бази.
            queryset = Source.objects.filter(owner=self.owner, status=Source.Status.ACTIVE).order_by("name")
        self.fields["sources"].queryset = queryset
        self.source_picker_options = self._build_source_picker_options(queryset)
        self.fields["time_window"].help_text = (
            "Можна обрати готовий інтервал або вказати конкретну дату чи період з дати до дати."
        )
        self.fields["exact_date"].help_text = "Заповнюється лише для режиму «Конкретна дата»."
        self.fields["date_from"].help_text = "Початок періоду для режиму «Період за датами»."
        self.fields["date_to"].help_text = "Кінець періоду для режиму «Період за датами»."
        self.fields["sources"].help_text = (
            "Можна вибрати конкретні джерела. Якщо список порожній, система використає всі активні джерела цього акаунта."
        )
        for date_field_name in ("exact_date", "date_from", "date_to"):
            self.fields[date_field_name].input_formats = ["%Y-%m-%d"]
            # Майбутні дати для моніторингу новин не мають сенсу,
            # тому одразу обмежуємо їх у браузерному календарі.
            self.fields[date_field_name].widget.attrs["max"] = today_iso
        if user_settings and not self.instance.pk:
            self.fields["time_window"].initial = user_settings.default_time_window

        # Зберігаємо режим поля в data-атрибуті, щоб у шаблоні просто приховувати зайві поля без складного JS.
        self.fields["exact_date"].widget.attrs["data-time-window-target"] = MonitoringTopic.TimeWindow.EXACT_DATE
        self.fields["date_from"].widget.attrs["data-time-window-target"] = MonitoringTopic.TimeWindow.DATE_RANGE
        self.fields["date_to"].widget.attrs["data-time-window-target"] = MonitoringTopic.TimeWindow.DATE_RANGE
        self.apply_styles()

    def _get_selected_source_ids(self):
        # Для власного picker-а визначаємо вибрані джерела окремо,
        # щоб коректно показувати стан і при створенні, і при редагуванні теми.
        if self.is_bound:
            if hasattr(self.data, "getlist"):
                selected_values = self.data.getlist(self.add_prefix("sources"))
            else:
                selected_values = self.data.get(self.add_prefix("sources"), [])
        elif self.instance.pk:
            selected_values = self.instance.sources.values_list("id", flat=True)
        else:
            selected_values = self.initial.get("sources", [])
        if isinstance(selected_values, (str, bytes)):
            selected_values = [selected_values]
        elif not isinstance(selected_values, (list, tuple, set)):
            try:
                selected_values = list(selected_values)
            except TypeError:
                selected_values = [selected_values]
        return {str(getattr(value, "pk", value)) for value in selected_values}

    def _build_source_picker_options(self, queryset):
        selected_ids = self._get_selected_source_ids()
        options = []
        for source in queryset:
            parsed_url = urlparse(source.url)
            options.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "type_label": source.get_type_display(),
                    "status": source.status,
                    "status_label": source.get_status_display(),
                    "display_url": parsed_url.netloc or source.url,
                    "is_selected": str(source.id) in selected_ids,
                }
            )
        return options

    def clean_sources(self):
        sources = self.cleaned_data["sources"]
        if self.owner and sources.exclude(owner=self.owner).exists():
            raise forms.ValidationError("Можна обирати лише власні джерела.")
        return sources

    def clean_topic(self):
        topic = (self.cleaned_data.get("topic") or "").strip()
        if self.owner:
            duplicate = MonitoringTopic.objects.filter(
                owner=self.owner,
                topic__iexact=topic,
            ).exclude(pk=self.instance.pk)
            # Дубль теми краще зловити ще на рівні форми,
            # щоб користувач побачив зрозумілу помилку в модалці, а не збій БД.
            if duplicate.exists():
                raise forms.ValidationError("Тема моніторингу з такою назвою вже існує.")
        return topic

    def clean_keywords(self):
        keywords = (self.cleaned_data.get("keywords") or "").strip()
        if not keywords:
            raise forms.ValidationError("Вкажіть ключові слова для теми моніторингу.")
        return keywords

    def clean(self):
        cleaned_data = super().clean()
        time_window = cleaned_data.get("time_window")
        exact_date = cleaned_data.get("exact_date")
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")

        if time_window == MonitoringTopic.TimeWindow.EXACT_DATE:
            cleaned_data["date_from"] = None
            cleaned_data["date_to"] = None

        elif time_window == MonitoringTopic.TimeWindow.DATE_RANGE:
            cleaned_data["exact_date"] = None

        else:
            cleaned_data["exact_date"] = None
            cleaned_data["date_from"] = None
            cleaned_data["date_to"] = None

        return cleaned_data


class DraftGenerationForm(StyledFormMixin, forms.Form):
    article = forms.ModelChoiceField(
        queryset=Article.objects.none(),
        empty_label="Оберіть новину",
        label="Новина-основа",
    )
    content_template = forms.ModelChoiceField(
        queryset=ContentTemplate.objects.none(),
        label="Шаблон",
        empty_label=None,
    )
    title = forms.CharField(
        label="Назва матеріалу",
        required=False,
        help_text="Якщо поле залишити порожнім, система сформує назву автоматично на основі новини.",
    )
    target_length = forms.ChoiceField(
        label="Довжина",
        choices=DraftLengthChoices.choices,
        initial=DraftLengthChoices.MEDIUM,
    )
    additional_instructions = forms.CharField(
        label="Додаткові вказівки",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        owner = kwargs.pop("owner", None)
        user_settings = kwargs.pop("user_settings", None)
        super().__init__(*args, **kwargs)
        if owner:
            self.fields["article"].queryset = (
                Article.objects.filter(owner=owner)
                .exclude(status=Article.Status.REJECTED)
                .select_related("source", "monitoring_topic")
                # Обрані новини показуємо першими, бо користувач уже відібрав їх для роботи.
                .order_by("-is_favorite", "-fetched_at")
            )
            # Для нової генерації показуємо лише активні шаблони.
            self.fields["content_template"].queryset = ContentTemplate.objects.filter(
                owner=owner,
                status=ContentTemplate.Status.ACTIVE,
            ).order_by("name")
        if user_settings:
            self.fields["target_length"].initial = user_settings.default_draft_length
        self.fields["title"].widget.attrs["placeholder"] = "Наприклад: OpenAI змінює правила гри на ринку ШІ"
        self.fields["additional_instructions"].widget.attrs["placeholder"] = (
            "За потреби вкажіть додатковий тон, акценти або формат подачі."
        )
        self.apply_styles()


class DraftEditForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Draft
        fields = ["title", "content"]
        widgets = {
            "content": forms.Textarea(attrs={"rows": 20}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_styles()


class ContentTemplateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ContentTemplate
        fields = ["name", "description", "prompt_text"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "prompt_text": forms.Textarea(attrs={"rows": 7}),
        }
        labels = {
            "name": "Назва шаблону",
            "description": "Короткий опис",
            "prompt_text": "Інструкція для генерації",
        }

    def __init__(self, *args, **kwargs):
        self.owner = kwargs.pop("owner", None)
        super().__init__(*args, **kwargs)
        self.fields["description"].required = False
        self.fields["description"].widget.attrs["placeholder"] = "Коротко поясніть, для чого потрібен цей шаблон."
        self.fields["prompt_text"].widget.attrs["placeholder"] = (
            "Опишіть структуру, тон, бажані акценти та вимоги до фінального матеріалу."
        )
        self.apply_styles()

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if self.owner:
            duplicate = ContentTemplate.objects.filter(owner=self.owner, name__iexact=name).exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise forms.ValidationError("Шаблон із такою назвою вже є у вашому акаунті.")
        return name

