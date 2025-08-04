import utils.blocks
import utils.manifests
from celery.utils.log import get_task_logger
from django.conf import settings
from django.db import transaction

from compute_horde_validator.celery import app
from compute_horde_validator.validator.locks import Lock, Locked, LockType
from compute_horde_validator.validator.models import SystemEvent

from . import utils

logger = get_task_logger(__name__)


@app.task(
    time_limit=utils.blocks.MAX_RUN_TIME + 30,
)
def scan_blocks_and_calculate_allowance():
    # TODO: write tests and add to celery beat config
    with transaction.atomic(using=settings.DEFAULT_DB_ALIAS):
        try:
            with Lock(LockType.ALLOWANCE_FETCHING, 5.0):
                utils.blocks.scan_blocks_and_calculate_allowance()
        except Locked:
            logger.debug("Another thread already fetching blocks")
        except utils.blocks.TimesUpError:
            scan_blocks_and_calculate_allowance.delay()


@app.task()
def sync_manifests():
    # TODO: write tests and add to celery beat config
    try:
        utils.manifests.sync_manifests()
    except Exception as e:
        msg = f"Failed to sync manifests: {e}"
        logger.error(msg, exc_info=True)
        SystemEvent.objects.create(
            type=SystemEvent.EventType.COMPUTE_TIME_ALLOWANCE,
            subtype=SystemEvent.EventSubType.FAILURE,
            data={
                "error": str(e),
            },
        )


# clean up old allowance blocks
# clean up old reservations - report system events if there are any expired ones (that were not undone)
