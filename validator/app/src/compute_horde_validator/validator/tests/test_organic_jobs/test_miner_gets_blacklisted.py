import pytest
from compute_horde.fv_protocol.facilitator_requests import V0JobCheated
from compute_horde.fv_protocol.validator_requests import JobStatusUpdate
from compute_horde.protocol_messages import (
    V0AcceptJobRequest,
    V0DeclineJobRequest,
    V0ExecutionDoneRequest,
    V0ExecutorFailedRequest,
    V0ExecutorReadyRequest,
    V0JobFailedRequest,
    V0JobFinishedRequest,
    V0VolumesReadyRequest,
)

from compute_horde_validator.validator.tests.helpers import patch_constance

# NOTE: In case this test is taking unreasonable amount of time before timing out:
# Something during the job flow is causing the execute_scenario timeout to be ignored.
# Maybe some cancellation gets eaten somewhere, or a timeout exception, no idea.
# The test will actually time out after one of these overridden timeouts though:
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.django_db(transaction=True),
    pytest.mark.override_config(
        DYNAMIC_ORGANIC_JOB_ALLOWED_LEEWAY_TIME=1,
        DYNAMIC_EXECUTOR_RESERVATION_TIME_LIMIT=1,
        DYNAMIC_EXECUTOR_STARTUP_TIME_LIMIT=1,
    ),
]


@pytest.mark.parametrize("reason", [*V0DeclineJobRequest.Reason])
async def test_miner_is_blacklisted__after_rejecting_job(
    job_request, another_job_request, faci_transport, miner_transport, execute_scenario, reason
):
    # Faci -> vali: V2 job request
    await faci_transport.add_message(job_request, send_before=0)

    # Vali -> miner: initial job request

    # Miner -> vali: rejects job
    accept_job_msg = V0DeclineJobRequest(job_uuid=job_request.uuid, reason=reason)
    await miner_transport.add_message(accept_job_msg, send_before=1)

    # Vali -> faci: job failed

    # Faci: request another job
    await faci_transport.add_message(another_job_request, send_before=2, sleep_before=0.2)

    # Vali -> faci: job rejected (no miners to take it)
    await execute_scenario(until=lambda: len(faci_transport.sent) >= 5)

    assert len(faci_transport.sent) == 5

    miner_rejected = JobStatusUpdate.model_validate_json(faci_transport.sent[-3])
    vali_rejected = JobStatusUpdate.model_validate_json(faci_transport.sent[-1])

    assert miner_rejected.status == "rejected"
    assert miner_rejected.metadata.comment.startswith(
        "Miner failed to excuse"
        if reason == V0DeclineJobRequest.Reason.BUSY
        else "Miner declined job"
    )
    assert miner_rejected.metadata.miner_response is not None

    assert vali_rejected.status == "rejected"
    assert vali_rejected.metadata.comment.startswith("No executor for job request")
    assert vali_rejected.metadata.miner_response is None


@pytest.mark.parametrize(
    "timeout_stage,expected_status_updates",
    [
        (
            0,
            [
                ("received", ""),
                ("failed", "timed out waiting for initial response"),
                ("received", ""),
                ("rejected", "No executor for job request"),
            ],
        ),
        (
            1,
            [
                ("received", ""),
                ("accepted", ""),
                ("failed", "timed out while preparing executor"),
                ("received", ""),
                ("rejected", "No executor for job request"),
            ],
        ),
        (
            2,
            [
                ("received", ""),
                ("accepted", ""),
                ("executor_ready", ""),
                ("failed", "timed out: FailureReason.VOLUMES_TIMED_OUT"),
                ("received", ""),
                ("rejected", "No executor for job request"),
            ],
        ),
    ],
)
async def test_miner_is_blacklisted__after_timing_out(
    job_request,
    another_job_request,
    faci_transport,
    miner_transport,
    execute_scenario,
    timeout_stage,
    expected_status_updates,
):
    await faci_transport.add_message(job_request, send_before=0)
    # Miner: timeout here (stage==0)

    if timeout_stage > 0:
        await miner_transport.add_message(
            V0AcceptJobRequest(job_uuid=job_request.uuid), send_before=1
        )
        # Miner: timeout here (stage==1)

    if timeout_stage > 1:
        await miner_transport.add_message(
            V0ExecutorReadyRequest(job_uuid=job_request.uuid), send_before=1
        )
        # Miner: timeout here (stage==2)

    # TODO: Timeout at new execution stages - volumes, execution etc.

    await faci_transport.add_message(
        another_job_request,
        send_before={0: 2, 1: 3, 2: 4}[
            timeout_stage
        ],  # The further we go, the more status updates go to the facilitator.
        sleep_before=0.2,
    )

    await execute_scenario(
        until=lambda: len(faci_transport.sent)
        >= len(expected_status_updates) + 1,  # +1 because auth message is also there
        timeout_seconds=3,
    )

    for i, (status, comment) in enumerate(expected_status_updates, start=1):
        status_message = JobStatusUpdate.model_validate_json(faci_transport.sent[i])
        assert status_message.status == status, (
            f"Bad message received at step {i}, expected {status} but got {status_message.status}"
        )
        assert comment in status_message.metadata.comment, (
            f"Failed checking message {i}, expected {comment} in comment, got {status_message.metadata.comment}"
        )


async def test_miner_is_blacklisted__after_failing_to_start_executor(
    job_request,
    another_job_request,
    faci_transport,
    miner_transport,
    execute_scenario,
):
    await faci_transport.add_message(job_request, send_before=0)

    accept_job_msg = V0AcceptJobRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(accept_job_msg, send_before=1)

    await miner_transport.add_message(
        V0ExecutorFailedRequest(job_uuid=job_request.uuid),
        send_before=1,
    )

    await faci_transport.add_message(
        another_job_request,
        send_before=2,  # job status=accepted, job status=failed
        sleep_before=0.2,
    )

    await execute_scenario(until=lambda: len(faci_transport.sent) >= 6, timeout_seconds=3)

    accepted_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[2])
    failed_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[3])
    rejected_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[5])

    assert accepted_status_msg.status == "accepted"
    assert failed_status_msg.status == "failed"
    assert rejected_status_msg.status == "rejected"


async def test_miner_is_blacklisted__after_failing_job(
    job_request,
    another_job_request,
    faci_transport,
    miner_transport,
    execute_scenario,
):
    await faci_transport.add_message(job_request, send_before=0)

    accept_job_msg = V0AcceptJobRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(accept_job_msg, send_before=1)

    executor_ready_msg = V0ExecutorReadyRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(executor_ready_msg, send_before=1)

    await miner_transport.add_message(
        V0JobFailedRequest(
            job_uuid=job_request.uuid,
            docker_process_exit_status=1,
            docker_process_stdout="stdout",
            docker_process_stderr="stderr",
        ),
        send_before=2,
    )

    await faci_transport.add_message(
        another_job_request,
        send_before=3,  # job status=accepted, job status=executor ready, job status=failed
        sleep_before=0.5,  # needed to ensure validator finishes the job flow
    )

    await execute_scenario(until=lambda: len(faci_transport.sent) >= 7, timeout_seconds=3)

    accepted_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[2])
    failed_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[4])
    rejected_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[6])

    assert accepted_status_msg.status == "accepted"
    assert failed_status_msg.status == "failed"
    assert rejected_status_msg.status == "rejected"


@patch_constance({"DYNAMIC_JOB_CHEATED_BLACKLIST_TIME_SECONDS": 5})
async def test_miner_is_blacklisted__after_job_reported_cheated(
    job_request,
    another_job_request,
    faci_transport,
    miner_transport,
    execute_scenario,
):
    await faci_transport.add_message(job_request, send_before=0)
    # vali -> miner: V0InitialJobRequest
    accept_job_msg = V0AcceptJobRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(accept_job_msg, send_before=1)
    # vali -> miner: job accepted receipt
    executor_ready_msg = V0ExecutorReadyRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(executor_ready_msg, send_before=1)
    # vali -> miner: V0JobRequest
    volumes_ready_msg = V0VolumesReadyRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(volumes_ready_msg, send_before=1)
    execution_done_msg = V0ExecutionDoneRequest(job_uuid=job_request.uuid)
    await miner_transport.add_message(execution_done_msg)
    # vali -> miner: job finished receipt

    completed_job_msg = V0JobFinishedRequest(
        job_uuid=job_request.uuid,
        docker_process_stdout="stdout",
        docker_process_stderr="stderr",
    )
    await miner_transport.add_message(completed_job_msg)

    # report previous job as cheated
    await faci_transport.add_message(
        V0JobCheated(job_uuid=job_request.uuid),
        send_before=2,  # job status=accepted, job status=failed
        sleep_before=0.2,  # needed to ensure validator finishes the job flow
    )

    await faci_transport.add_message(
        another_job_request,
        send_before=0,
    )

    await execute_scenario(until=lambda: len(faci_transport.sent) >= 9, timeout_seconds=3)

    accepted_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[2])
    finished_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[6])
    rejected_status_msg = JobStatusUpdate.model_validate_json(faci_transport.sent[8])

    assert accepted_status_msg.status == "accepted"
    assert finished_status_msg.status == "completed"
    assert rejected_status_msg.status == "rejected"
