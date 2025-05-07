import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import assert_never

from channels.layers import get_channel_layer
from compute_horde.fv_protocol.facilitator_requests import OrganicJobRequest, V2JobRequest
from compute_horde.fv_protocol.validator_requests import (
    JobStatusMetadata,
    JobStatusType,
    JobStatusUpdate,
    MinerResponse,
)
from compute_horde.miner_client.organic import (
    FailureReason,
    OrganicJobDetails,
    OrganicJobError,
    run_organic_job,
)
from compute_horde.protocol_messages import (
    V0AcceptJobRequest,
    V0DeclineJobRequest,
    V0JobFailedRequest,
)
from compute_horde.receipts.models import JobStartedReceipt
from compute_horde_core.executor_class import ExecutorClass
from django.conf import settings
from pydantic import JsonValue

from compute_horde_validator.validator import job_excuses
from compute_horde_validator.validator.dynamic_config import aget_config
from compute_horde_validator.validator.models import (
    AdminJobRequest,
    MetagraphSnapshot,
    Miner,
    OrganicJob,
    SystemEvent,
)
from compute_horde_validator.validator.organic_jobs.miner_client import MinerClient
from compute_horde_validator.validator.utils import TRUSTED_MINER_FAKE_KEY

logger = logging.getLogger(__name__)

MINER_CLIENT_CLASS = MinerClient


def status_update_from_job(
    job: OrganicJob,
    status: JobStatusType,
    message_type: str | None = None,
) -> JobStatusUpdate:
    miner_response = MinerResponse(
        job_uuid=str(job.job_uuid),
        message_type=message_type,
        docker_process_stdout=job.stdout,
        docker_process_stderr=job.stderr,
        artifacts=job.artifacts,
    )
    metadata = JobStatusMetadata(
        comment=job.comment,
        miner_response=miner_response,
    )
    return JobStatusUpdate(
        uuid=str(job.job_uuid),
        status=status,
        metadata=metadata,
    )


async def save_job_execution_event(
    subtype: str, long_description: str, data: JsonValue = None, success: bool = False
) -> None:
    await SystemEvent.objects.using(settings.DEFAULT_DB_ALIAS).acreate(
        type=SystemEvent.EventType.MINER_ORGANIC_JOB_SUCCESS
        if success
        else SystemEvent.EventType.MINER_ORGANIC_JOB_FAILURE,
        subtype=subtype,
        long_description=long_description,
        data=data or {},
    )


async def _dummy_notify_callback(_: JobStatusUpdate) -> None:
    pass


async def _get_current_block() -> int:
    return (await MetagraphSnapshot.aget_latest()).block


async def execute_organic_job_request(job_request: OrganicJobRequest, miner: Miner) -> OrganicJob:
    if (
        miner.hotkey == settings.DEBUG_MINER_KEY
        and settings.DEBUG_MINER_ADDRESS
        and settings.DEBUG_MINER_PORT
    ):
        miner_ip = settings.DEBUG_MINER_ADDRESS
        miner_port = settings.DEBUG_MINER_PORT
        ip_type = 4
        on_trusted_miner = False
    elif miner.hotkey == TRUSTED_MINER_FAKE_KEY:
        miner_ip = settings.TRUSTED_MINER_ADDRESS
        miner_port = settings.TRUSTED_MINER_PORT
        ip_type = 4
        on_trusted_miner = True
    else:
        miner_ip = miner.address
        miner_port = miner.port
        ip_type = miner.ip_version
        on_trusted_miner = False

    if settings.DEBUG_USE_MOCK_BLOCK_NUMBER:
        block = 5136476 + int((time.time() - 1742076533) / 12)
    else:
        block = await _get_current_block()

    job = await OrganicJob.objects.acreate(
        job_uuid=str(job_request.uuid),
        miner=miner,
        miner_address=miner_ip,
        miner_address_ip_version=ip_type,
        miner_port=miner_port,
        executor_class=job_request.executor_class,
        job_description="User job from facilitator",
        block=block,
        on_trusted_miner=on_trusted_miner,
    )

    miner_client = MINER_CLIENT_CLASS(
        miner_hotkey=miner.hotkey,
        miner_address=job.miner_address,
        miner_port=job.miner_port,
        job_uuid=str(job.job_uuid),
        my_keypair=settings.BITTENSOR_WALLET().hotkey,
    )

    async def job_status_callback(status_update: JobStatusUpdate):
        logger.debug("Job status update: %s", status_update)
        await get_channel_layer().send(
            f"job_status_updates__{status_update.uuid}",
            {"type": "job_status_update", "payload": status_update.model_dump()},
        )

    total_job_timeout = await aget_config("DYNAMIC_ORGANIC_JOB_TIMEOUT")
    initial_response_timeout = await aget_config("DYNAMIC_ORGANIC_JOB_INITIAL_RESPONSE_TIMEOUT")
    executor_ready_timeout = await aget_config("DYNAMIC_ORGANIC_JOB_EXECUTOR_READY_TIMEOUT")
    await drive_organic_job(
        miner_client,
        job,
        job_request,
        total_job_timeout=total_job_timeout,
        initial_response_timeout=initial_response_timeout,
        executor_ready_timeout=executor_ready_timeout,
        notify_callback=job_status_callback,
    )

    return job


async def drive_organic_job(
    miner_client: MinerClient,
    job: OrganicJob,
    job_request: OrganicJobRequest | AdminJobRequest,
    total_job_timeout: int = 300,
    initial_response_timeout: int = 3,
    executor_ready_timeout: int = 300,
    notify_callback: Callable[[JobStatusUpdate], Awaitable[None]] = _dummy_notify_callback,
) -> bool:
    """
    Execute an organic job on a miner client.
    Returns True if the job was successfully executed, False otherwise.
    """

    if job.on_trusted_miner and await aget_config("DYNAMIC_DISABLE_TRUSTED_ORGANIC_JOB_EVENTS"):
        # ignore trusted system events
        async def save_event(*args, **kwargs):
            pass
    else:
        data: JsonValue = {"job_uuid": str(job.job_uuid), "miner_hotkey": miner_client.my_hotkey}
        save_event = partial(save_job_execution_event, data=data)

    async def notify_job_accepted(msg: V0AcceptJobRequest) -> None:
        await notify_callback(status_update_from_job(job, "accepted", msg.message_type))

    miner_client.notify_job_accepted = notify_job_accepted  # type: ignore[method-assign]
    # TODO: remove method assignment above and properly handle notify_* cases

    artifacts_dir = job_request.artifacts_dir if isinstance(job_request, V2JobRequest) else None
    job_details = OrganicJobDetails(
        job_uuid=str(job.job_uuid),  # TODO: fix uuid field in AdminJobRequest
        executor_class=ExecutorClass(job_request.executor_class),
        docker_image=job_request.docker_image,
        docker_run_options_preset="nvidia_all" if job_request.use_gpu else "none",
        docker_run_cmd=job_request.get_args(),
        total_job_timeout=total_job_timeout,
        volume=job_request.volume,
        output=job_request.output_upload,
        artifacts_dir=artifacts_dir,
    )

    try:
        stdout, stderr, artifacts = await run_organic_job(
            miner_client,
            job_details,
            initial_response_timeout=initial_response_timeout,
            executor_ready_timeout=executor_ready_timeout,
        )

        comment = f"Miner {miner_client.miner_name} finished: {stdout=} {stderr=}"
        job.stdout = stdout
        job.stderr = stderr
        job.artifacts = artifacts
        job.status = OrganicJob.Status.COMPLETED
        job.comment = comment
        await job.asave()
        logger.info(comment)
        await save_event(
            subtype=SystemEvent.EventSubType.SUCCESS, long_description=comment, success=True
        )
        await notify_callback(status_update_from_job(job, "completed", "V0JobFinishedRequest"))
        return True

    except OrganicJobError as exc:
        if exc.reason == FailureReason.MINER_CONNECTION_FAILED:
            comment = f"Miner connection error: {exc}"
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.warning(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.MINER_CONNECTION_ERROR, long_description=comment
            )
            await notify_callback(status_update_from_job(job, status="failed"))

        elif exc.reason == FailureReason.INITIAL_RESPONSE_TIMED_OUT:
            comment = f"Miner {miner_client.miner_name} timed out waiting for initial response {job.job_uuid} after {initial_response_timeout} seconds"
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.warning(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.JOB_NOT_STARTED,
                long_description=comment,
            )
            await notify_callback(status_update_from_job(job, "failed"))

        elif (
            exc.reason == FailureReason.JOB_DECLINED
            and isinstance(exc.received, V0DeclineJobRequest)
            and exc.received.reason == V0DeclineJobRequest.Reason.BUSY
        ):
            # Check when the job was requested to validate excuses against that timestamp
            job_request_time = (
                await JobStartedReceipt.objects.aget(job_uuid=job.job_uuid)
            ).timestamp
            valid_excuses = await job_excuses.filter_valid_excuse_receipts(
                receipts_to_check=exc.received.receipts or [],
                check_time=job_request_time,
                declined_job_uuid=str(job.job_uuid),
                declined_job_executor_class=ExecutorClass(job.executor_class),
                declined_job_is_synthetic=False,
                minimum_validator_stake_for_excuse=await aget_config(
                    "DYNAMIC_MINIMUM_VALIDATOR_STAKE_FOR_EXCUSE"
                ),
                miner_hotkey=job.miner.hotkey,
            )
            expected_executor_count = await job_excuses.get_expected_miner_executor_count(
                check_time=job_request_time,
                miner_hotkey=job.miner.hotkey,
                executor_class=ExecutorClass(job.executor_class),
            )
            if len(valid_excuses) >= expected_executor_count:
                comment = (
                    f"Miner properly excused job {miner_client.miner_name}: {exc.received_str()}"
                )
                job.status = OrganicJob.Status.EXCUSED
                job.comment = comment
                await job.asave()
                logger.info(comment)
                await save_event(
                    subtype=SystemEvent.EventSubType.JOB_EXCUSED,
                    long_description=comment,
                )
                await notify_callback(status_update_from_job(job, "rejected"))
            else:
                comment = (
                    f"Miner failed to excuse job {miner_client.miner_name}: {exc.received_str()}"
                )
                job.status = OrganicJob.Status.FAILED
                job.comment = comment
                await job.asave()
                logger.info(comment)
                await save_event(
                    subtype=SystemEvent.EventSubType.JOB_REJECTED,
                    long_description=comment,
                )
                await notify_callback(status_update_from_job(job, "rejected"))

        elif exc.reason == FailureReason.JOB_DECLINED:
            comment = f"Miner declined job {miner_client.miner_name}: {exc.received_str()}"
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.info(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.JOB_REJECTED,
                long_description=comment,
            )
            await notify_callback(status_update_from_job(job, "rejected"))

        elif exc.reason == FailureReason.EXECUTOR_READINESS_RESPONSE_TIMED_OUT:
            comment = f"Miner {miner_client.miner_name} timed out while preparing executor for job {job.job_uuid} after {executor_ready_timeout} seconds"
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.warning(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.JOB_NOT_STARTED,
                long_description=comment,
            )
            await notify_callback(status_update_from_job(job, "failed"))

        elif exc.reason == FailureReason.STREAMING_JOB_READY_TIMED_OUT:
            comment = (
                f"Streaming job {job.job_uuid} not ready after {executor_ready_timeout} seconds"
            )
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.warning(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.JOB_NOT_STARTED,
                long_description=comment,
            )
            await notify_callback(status_update_from_job(job, "failed"))

        elif exc.reason == FailureReason.EXECUTOR_FAILED:
            comment = (
                f"Miner {miner_client.miner_name} failed to start executor: {exc.received_str()}"
            )
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.info(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.JOB_REJECTED,
                long_description=comment,
            )
            await notify_callback(status_update_from_job(job, "failed"))

        elif exc.reason == FailureReason.FINAL_RESPONSE_TIMED_OUT:
            comment = f"Miner {miner_client.miner_name} timed out after {total_job_timeout} seconds"
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.warning(comment)
            await save_event(
                subtype=SystemEvent.EventSubType.JOB_EXECUTION_TIMEOUT, long_description=comment
            )
            await notify_callback(status_update_from_job(job, "failed"))

        elif exc.reason == FailureReason.JOB_FAILED:
            comment = f"Miner {miner_client.miner_name} failed: {exc.received_str()}"
            subtype = SystemEvent.EventSubType.FAILURE
            if isinstance(exc.received, V0JobFailedRequest):
                job.stdout = exc.received.docker_process_stdout
                job.stderr = exc.received.docker_process_stderr
                job.error_type = exc.received.error_type
                job.error_detail = exc.received.error_detail
                match exc.received.error_type:
                    case None:
                        pass
                    case V0JobFailedRequest.ErrorType.HUGGINGFACE_DOWNLOAD:
                        subtype = SystemEvent.EventSubType.ERROR_DOWNLOADING_FROM_HUGGINGFACE
                    case _:
                        assert_never(exc.received.error_type)
            job.status = OrganicJob.Status.FAILED
            job.comment = comment
            await job.asave()
            logger.info(comment)
            await save_event(subtype=subtype, long_description=comment)
            await notify_callback(status_update_from_job(job, "failed", "V0JobFailedRequest"))

        else:
            assert_never(exc.reason)
        return False
