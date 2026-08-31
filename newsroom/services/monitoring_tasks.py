from __future__ import annotations

from datetime import timedelta
import logging
import subprocess
import sys
from pathlib import Path

from django.contrib.auth.models import User
from django.db import models
from django.db import close_old_connections
from django.utils import timezone

from ..models import ActivityEvent, MonitoringTopic
from .fetcher import run_monitoring_for_topic


MONITORING_RUN_STALE_AFTER = timedelta(hours=2)
MONITORING_CANCEL_STALE_AFTER = timedelta(minutes=3)
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANAGE_PY = PROJECT_ROOT / "manage.py"


def reset_stale_monitoring_runs(owner=None) -> int:
    now = timezone.now()
    stale_run_cutoff = now - MONITORING_RUN_STALE_AFTER
    cancelled_run_cutoff = now - MONITORING_CANCEL_STALE_AFTER
    queryset = MonitoringTopic.objects.filter(
        run_state=MonitoringTopic.RunState.RUNNING,
    ).filter(
        models.Q(run_started_at__lt=stale_run_cutoff)
        | models.Q(cancel_requested=True, run_started_at__lt=cancelled_run_cutoff)
    )
    if owner is not None:
        queryset = queryset.filter(owner=owner)

    # Якщо сервер перезапустився, daemon-потік моніторингу міг завершитися
    # без запису фінального стану. Такі старі запуски не повинні блокувати тему.
    return queryset.update(
        run_state=MonitoringTopic.RunState.ERROR,
        cancel_requested=False,
    )


def start_monitoring_in_background(topic: MonitoringTopic, actor: User | None = None) -> bool:
    reset_stale_monitoring_runs(owner=topic.owner)
    started = MonitoringTopic.objects.filter(
        id=topic.id,
        owner=topic.owner,
        status=MonitoringTopic.Status.ACTIVE,
        run_state__in=[MonitoringTopic.RunState.IDLE, MonitoringTopic.RunState.ERROR],
    ).update(
        run_state=MonitoringTopic.RunState.RUNNING,
        run_started_at=timezone.now(),
        cancel_requested=False,
    )
    if not started:
        return False

    ActivityEvent.log(
        owner=topic.owner,
        description=f"Запущено моніторинг теми: {topic.topic}",
    )

    command = [
        sys.executable,
        str(MANAGE_PY),
        "run_monitoring_topic",
        str(topic.id),
    ]
    if actor:
        command.extend(["--actor-id", str(actor.id)])

    # Запускаємо окремий процес, а не daemon-потік у web-запиті.
    # Так моніторинг не обривається разом із відповіддю сервера.
    subprocess.Popen(command, cwd=str(PROJECT_ROOT))
    return True


def request_monitoring_stop(topic: MonitoringTopic) -> bool:
    updated = MonitoringTopic.objects.filter(
        id=topic.id,
        owner=topic.owner,
        run_state=MonitoringTopic.RunState.RUNNING,
        cancel_requested=False,
    ).update(
        cancel_requested=True,
    )
    if not updated:
        return False

    ActivityEvent.log(
        owner=topic.owner,
        description=f"Запитано зупинку моніторингу теми: {topic.topic}",
    )
    return True


def _run_monitoring_job(topic_id: int, actor_id: int | None = None) -> None:
    close_old_connections()
    try:
        topic = MonitoringTopic.objects.select_related("owner").get(id=topic_id)
        actor = User.objects.filter(id=actor_id).first() if actor_id else None

        try:
            run_monitoring_for_topic(topic, actor=actor)
        except Exception as error:
            logger.exception("Фоновий моніторинг завершився з помилкою.")
            error_message = f"Фоновий моніторинг завершився з помилкою: {str(error)[:180]}"
            MonitoringTopic.objects.filter(id=topic_id).update(
                run_state=MonitoringTopic.RunState.ERROR,
                cancel_requested=False,
            )
            ActivityEvent.log(
                owner=topic.owner,
                description=error_message,
            )
            return

        MonitoringTopic.objects.filter(id=topic_id).update(
            run_state=MonitoringTopic.RunState.IDLE,
            cancel_requested=False,
        )
    finally:
        close_old_connections()
