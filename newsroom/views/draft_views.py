from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from ..forms import DraftEditForm, DraftGenerationForm
from ..models import ActivityEvent, Article, ContentTemplate, Draft, MonitoringTopic
from ..services.docx_export import build_draft_docx
from ..services.drafts import DraftGenerationError, build_default_title_from_article, generate_draft_from_article
from ..session_state import get_current_member, get_user_settings
from .helpers import filter_casefold_search, get_request_next


@login_required
@require_http_methods(["GET", "POST"])
def drafts_page(request):
    actor = get_current_member(request)
    user_settings = get_user_settings(request.user)
    page_number = request.GET.get("page", 1)
    preselected_article_id = request.GET.get("article", "")
    preselected_template_id = request.GET.get("template", "")

    content_templates = ContentTemplate.objects.filter(
        owner=request.user,
        status=ContentTemplate.Status.ACTIVE,
    ).order_by("name")
    has_content_templates = content_templates.exists()
    preselected_article = (
        Article.objects.filter(owner=request.user)
        .exclude(status=Article.Status.REJECTED)
        .select_related("source", "monitoring_topic")
        .filter(id=preselected_article_id)
        .first()
        if preselected_article_id
        else None
    )
    preselected_template = content_templates.filter(id=preselected_template_id).first() if preselected_template_id else None

    initial_generation_data = {}
    if preselected_article:
        initial_generation_data["article"] = preselected_article
        initial_generation_data["title"] = build_default_title_from_article(preselected_article)
    if preselected_template:
        initial_generation_data["content_template"] = preselected_template

    generation_form = DraftGenerationForm(
        owner=request.user,
        user_settings=user_settings,
        initial=initial_generation_data,
    )

    if request.method == "POST":
        form_type = request.POST.get("form_type")

        if form_type == "generate_content":
            generation_form = DraftGenerationForm(
                request.POST,
                owner=request.user,
                user_settings=user_settings,
            )
            if not has_content_templates:
                generation_form.add_error("content_template", "Спочатку створіть хоча б один активний шаблон контенту.")
            if generation_form.is_valid():
                try:
                    draft = generate_draft_from_article(
                        generation_form.cleaned_data["article"],
                        content_template=generation_form.cleaned_data["content_template"],
                        target_length=generation_form.cleaned_data["target_length"],
                        additional_instructions=generation_form.cleaned_data["additional_instructions"],
                        custom_title=generation_form.cleaned_data["title"],
                        current_user=actor,
                    )
                except DraftGenerationError as exc:
                    # Генерація залежить від OpenAI API, тому при помилці
                    # показуємо користувачу чесне й зрозуміле повідомлення.
                    messages.error(request, str(exc))
                else:
                    messages.success(request, "Матеріал успішно згенеровано.")
                    return redirect(f"{reverse('newsroom:drafts')}?content={draft.id}")
            messages.error(request, "Не вдалося згенерувати матеріал. Перевірте поля форми.")

    queryset = (
        Draft.objects.filter(owner=request.user)
        .select_related(
            "source_article",
            "source_article__source",
            "source_article__monitoring_topic",
            "content_template",
        )
        .order_by("-updated_at")
    )
    paginator = Paginator(queryset, 20)
    page_obj = paginator.get_page(page_number)
    selected_draft_id = request.GET.get("content", "")
    selected_draft = queryset.filter(id=selected_draft_id).first() if selected_draft_id else None
    if not selected_draft:
        selected_draft = next(iter(page_obj.object_list), None)

    context = {
        "page_title": "Генерація контенту",
        "generation_form": generation_form,
        "has_content_templates": has_content_templates,
        "selected_draft": selected_draft,
        "page_obj": page_obj,
    }
    return render(request, "newsroom/drafts.html", context)


@login_required
@require_GET
def generated_materials_page(request):
    template_filter = request.GET.get("template", "")
    topic_filter = request.GET.get("topic", "")
    period_filter = request.GET.get("period", "")
    search_query = request.GET.get("q", "").strip()
    sort_filter = request.GET.get("sort", "new")
    selected_tab = request.GET.get("tab", "result")
    selected_draft_id = request.GET.get("content", "")
    page_number = request.GET.get("page", 1)

    queryset = (
        Draft.objects.filter(owner=request.user)
        .select_related(
            "source_article",
            "source_article__source",
            "source_article__monitoring_topic",
            "content_template",
        )
    )

    if template_filter:
        queryset = queryset.filter(content_template_id=template_filter)
    if topic_filter:
        queryset = queryset.filter(source_article__monitoring_topic_id=topic_filter)
    period_start = None
    if period_filter == "today":
        period_start = timezone.now() - timedelta(days=1)
    elif period_filter == "week":
        period_start = timezone.now() - timedelta(days=7)
    elif period_filter == "month":
        period_start = timezone.now() - timedelta(days=30)
    if period_start:
        queryset = queryset.filter(updated_at__gte=period_start)

    if sort_filter == "old":
        queryset = queryset.order_by("updated_at")
    elif sort_filter == "title":
        queryset = queryset.order_by("title")
    else:
        sort_filter = "new"
        queryset = queryset.order_by("-updated_at")

    drafts = list(queryset)
    if search_query:
        drafts = filter_casefold_search(
            drafts,
            search_query,
            ("title", "content", "source_article__title"),
        )

    total_count = len(drafts)
    paginator = Paginator(drafts, 10)
    page_obj = paginator.get_page(page_number)

    selected_draft = next((draft for draft in drafts if str(draft.id) == str(selected_draft_id)), None) if selected_draft_id else None
    if not selected_draft:
        selected_draft = next(iter(page_obj.object_list), None)

    related_drafts = Draft.objects.none()
    if selected_draft and selected_draft.source_article_id:
        # Показуємо не історію редагування, а інші матеріали,
        # згенеровані з тієї самої вихідної новини.
        related_drafts = (
            Draft.objects.filter(owner=request.user, source_article_id=selected_draft.source_article_id)
            .exclude(id=selected_draft.id)
            .select_related("content_template")
            .order_by("-updated_at")[:6]
        )

    context = {
        "page_title": "Згенеровані матеріали",
        "page_obj": page_obj,
        "total_count": total_count,
        "selected_draft": selected_draft,
        "related_drafts": related_drafts,
        "content_templates": ContentTemplate.objects.filter(owner=request.user).order_by("status", "name"),
        "topic_filters": MonitoringTopic.objects.filter(owner=request.user).order_by("topic"),
        "template_filter": template_filter,
        "topic_filter": topic_filter,
        "period_filter": period_filter,
        "search_query": search_query,
        "sort_filter": sort_filter,
        "selected_tab": selected_tab if selected_tab in {"result", "source", "related"} else "result",
    }
    return render(request, "newsroom/generated_materials.html", context)


@login_required
@require_POST
def delete_draft(request, draft_id: int):
    draft = get_object_or_404(Draft, owner=request.user, id=draft_id)
    draft_title = draft.title

    # Видаляємо лише матеріал поточного користувача, щоб один редактор
    # не міг випадково або навмисно прибрати чужу згенеровану чернетку.
    draft.delete()

    ActivityEvent.log(
        owner=request.user,
        description=f"Видалено згенерований матеріал: {draft_title}",
    )
    messages.success(request, "Згенерований матеріал видалено.")

    next_target = get_request_next(request)
    detail_path = reverse("newsroom:draft_detail", args=[draft_id])
    if next_target and detail_path not in next_target:
        return redirect(next_target)
    return redirect("newsroom:generated_materials")


@login_required
@require_http_methods(["GET", "POST"])
def draft_detail(request, draft_id: int):
    draft = get_object_or_404(
        Draft.objects.filter(owner=request.user).select_related(
            "source_article",
            "source_article__source",
            "source_article__monitoring_topic",
            "content_template",
        ),
        id=draft_id,
    )

    if request.method == "POST":
        form = DraftEditForm(request.POST, instance=draft)
        if form.is_valid():
            updated_draft = form.save(commit=False)
            # Оновлюємо тільки ті поля, які редактор змінює на сторінці деталей чернетки.
            # updated_at також включаємо явно, бо це поле автоматично оновлюється через auto_now.
            updated_draft.save(update_fields=["title", "content", "updated_at"])
            messages.success(request, "Чернетку збережено.")
            return redirect("newsroom:draft_detail", draft_id=draft.id)
        messages.error(request, "Не вдалося зберегти зміни.")
    else:
        form = DraftEditForm(instance=draft)

    context = {
        "page_title": draft.title,
        "draft": draft,
        "form": form,
    }
    return render(request, "newsroom/draft_detail.html", context)


@login_required
@require_GET
def export_draft_docx(request, draft_id: int):
    draft = get_object_or_404(
        Draft.objects.filter(owner=request.user).select_related(
            "source_article",
            "source_article__source",
            "source_article__monitoring_topic",
            "content_template",
        ),
        id=draft_id,
    )
    docx_content = build_draft_docx(draft)
    filename_base = slugify(draft.title, allow_unicode=True) or f"generated-content-{draft.id}"
    filename = f"{filename_base}.docx"

    response = HttpResponse(
        docx_content,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    # filename* коректно передає українські назви файлів у сучасних браузерах.
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response
