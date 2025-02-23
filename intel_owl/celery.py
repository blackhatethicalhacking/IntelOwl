# This file is a part of IntelOwl https://github.com/intelowlproject/IntelOwl
# See the file 'LICENSE' for copying permission.

from __future__ import absolute_import, unicode_literals

import os
import uuid
from typing import Dict

from celery import Celery
from celery.schedules import crontab
from django.conf import settings
from kombu import Exchange, Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "intel_owl.settings")

app = Celery("intel_owl")

if settings.AWS_SQS:
    if settings.STAGE_PRODUCTION:
        SQS_QUEUE = "prod"
    elif settings.STAGE_STAGING:
        SQS_QUEUE = "stag"
    else:
        SQS_QUEUE = "local"

    PREDEFINED_QUEUES = {
        queue: {
            "url": f"https://sqs.{settings.AWS_REGION}"
            f".amazonaws.com/{settings.AWS_USER_NUMBER}/"
            f"intelowl-{SQS_QUEUE}-{queue}"
        }
        for queue in settings.CELERY_QUEUES
    }
    # in this way they are printed in the Docker logs
    print(f"predefined queues active: {PREDEFINED_QUEUES}")

    BROKER_TRANSPORT_OPTIONS = {
        "region": settings.AWS_REGION,
        "predefined_queues": PREDEFINED_QUEUES,
        "max_retries": 0,
        "polling_interval": 2,
        # this is the highest possible value
        # https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/sqs.html#caveats
        # this must be longer than the longest possible task we execute
        "visibility_timeout": 43200,
    }
    if not settings.AWS_IAM_ACCESS:
        BROKER_TRANSPORT_OPTIONS["access_key_id"] = settings.AWS_ACCESS_KEY_ID
        BROKER_TRANSPORT_OPTIONS["secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
else:
    BROKER_TRANSPORT_OPTIONS = {}

DEFAULT_QUEUE = settings.CELERY_QUEUES[0]

app.conf.update(
    task_default_queue=DEFAULT_QUEUE,
    task_queues=[
        Queue(
            key,
            Exchange(key),
            routing_key=key,
        )
        for key in settings.CELERY_QUEUES
    ],
    task_time_limit=1800,
    broker_url=settings.BROKER_URL,
    result_backend=settings.RESULT_BACKEND,
    accept_content=["application/json"],
    task_serializer="json",
    result_serializer="json",
    imports=("intel_owl.tasks",),
    worker_redirect_stdouts=False,
    worker_hijack_root_logger=False,
    # this is to avoid RAM issues caused by long usage of this tool
    # changing the child saves from memory leaks but is CPU intensive...so care
    worker_max_tasks_per_child=1000,
    # name is in kilobytes
    # https://medium.com/squad-engineering
    # /two-years-with-celery-in-production-bug-fix-edition-22238669601d
    # they suggest to remove this but well...maybe just put this a huge number
    # there are workers in the primary queue that use a lot of memory
    worker_max_memory_per_child=3000000,
    worker_proc_alive_timeout=20,
    # these two are needed to enable priority and correct tasks execution
    # see: https://docs.celeryq.dev/en/stable
    # /userguide/optimizing.html#reserve-one-task-at-a-time
    # UPDATE: we disabled task_acks_late because not useful.
    # We don't need to acks late because we don't want to re-get the same message
    # task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_transport_options=BROKER_TRANSPORT_OPTIONS,
    # The remote control command pool_restart sends restart requests
    # to the workers child processes.
    # It is particularly useful for forcing the worker to import new modules,
    # or for reloading already imported modules.
    # This command does not interrupt executing tasks.
    worker_pool_restarts=True,
    # required for code-coverage to work properly in tests
    task_always_eager=settings.STAGE_CI,
)

app.autodiscover_tasks()

app.conf.beat_schedule = {
    # execute sometimes to cleanup old jobs
    "remove_old_jobs": {
        "task": "intel_owl.tasks.remove_old_jobs",
        "schedule": crontab(minute=10, hour=2),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # execute sometimes to cleanup stuck analysis
    "check_stuck_analysis": {
        "task": "intel_owl.tasks.check_stuck_analysis",
        "schedule": crontab(minute="*/5"),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # Executes only on Wed because on Tue it's updated
    "maxmind_updater": {
        "task": "intel_owl.tasks.maxmind_updater",
        "schedule": crontab(minute=0, hour=1, day_of_week=3),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # execute every 6 hours
    "talos_updater": {
        "task": "intel_owl.tasks.talos_updater",
        "schedule": crontab(minute=5, hour="*/6"),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # execute every 10 minutes
    "tor_updater": {
        "task": "intel_owl.tasks.tor_updater",
        "schedule": crontab(minute="*/10"),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # yara repo updater 1 time a day
    "yara_updater": {
        "task": "intel_owl.tasks.yara_updater",
        "schedule": crontab(minute=0, hour=0),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # quark rules updater 2 time a week
    "quark_updater": {
        "task": "intel_owl.tasks.quark_updater",
        "schedule": crontab(minute=0, hour=0, day_of_week=[2, 5]),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
    # quark rules updater 2 time a week
    "update_notifications_with_releases": {
        "task": "intel_owl.tasks.update_notifications_with_releases",
        "schedule": crontab(minute=0, hour=22),
        "options": {
            "queue": DEFAULT_QUEUE,
            "MessageGroupId": str(uuid.uuid4()),
        },
    },
}


def broadcast(function: str, queue: str = None, arguments: Dict = None):
    if queue:
        if queue not in settings.CELERY_QUEUES:
            queue = DEFAULT_QUEUE
        queue = [f"celery@worker_{queue}"]
    app.control.broadcast(function, destination=queue, arguments=arguments)
