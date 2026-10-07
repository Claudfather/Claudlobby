"""Host activate waits for running jobs instead of refusing, and they back off meanwhile (#2208).

Every composed timer job, host operation and native shell holds state/activation.lock
shared while it runs, and an activation takes it exclusively. An activation first holds
state/activation-pending.lock exclusively, until it ends, so a job that starts while it
waits sees the activation as already running.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import threading
import time

import pytest

from claudlobby import activation_state as a
from claudlobby.runtime_admission import _take_shared


def _root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/activation.lock").touch()
    return tmp_path


def _hold(path: Path, how: int) -> int:
    """Another holder of *path*: an open description that takes *how* now."""
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, how | fcntl.LOCK_NB)
    return fd


def _release_after(fd: int, seconds: float) -> threading.Timer:
    timer = threading.Timer(seconds, os.close, (fd,))
    timer.start()
    return timer


def test_an_activation_waits_for_a_running_job_then_takes_the_lock(tmp_path):
    root = _root(tmp_path)
    job = _release_after(_hold(root / "state/activation.lock", fcntl.LOCK_SH), 1.0)
    started = time.monotonic()
    with a.locked_activation(root, wait=30) as store:
        waited = time.monotonic() - started
        store.assert_locked()
    job.join()
    assert 0.9 <= waited < 10, waited


def test_a_job_starting_while_an_activation_waits_backs_off_until_it_ends(tmp_path):
    root = _root(tmp_path)
    running = _hold(root / "state/activation.lock", fcntl.LOCK_SH)
    inside, leave = threading.Event(), threading.Event()

    def activate():
        with a.locked_activation(root, wait=30):
            inside.set()
            leave.wait(30)

    activation = threading.Thread(target=activate)
    activation.start()
    try:
        deadline = time.monotonic() + 10
        while not a._exclusively_held(root / "state/activation-pending.lock"):
            assert time.monotonic() < deadline, (
                "the activation never marked itself pending"
            )
            time.sleep(0.01)
        late = os.open(root / "state/activation.lock", os.O_RDONLY)
        with pytest.raises(BlockingIOError):
            _take_shared(root, late)  # pending: a job starting now backs off
        assert not inside.is_set()
        os.close(running)  # the job that was already running finishes
        assert inside.wait(10), (
            "the activation did not take the lock once the job finished"
        )
        with pytest.raises(BlockingIOError):
            _take_shared(root, late)  # running
    finally:
        leave.set()
        activation.join(30)
    _take_shared(root, late)  # ended: jobs start again
    os.close(late)


@pytest.mark.parametrize("held", ["activation.lock", "activation-pending.lock"])
def test_another_activation_running_or_pending_refuses_at_once(tmp_path, held):
    root = _root(tmp_path)
    other = _hold(root / "state" / held, fcntl.LOCK_EX)
    started = time.monotonic()
    with pytest.raises(a.ActivationError) as refused:
        with a.locked_activation(root, wait=30):
            pytest.fail("two activations held the lock")
    assert time.monotonic() - started < 5
    assert str(refused.value) == a.ANOTHER_ACTIVATION and a.lock_busy(
        str(refused.value)
    )
    os.close(other)


def test_a_job_that_outlasts_the_wait_is_named_and_jobs_start_again(tmp_path):
    root = _root(tmp_path)
    job = _hold(root / "state/activation.lock", fcntl.LOCK_SH)
    started = time.monotonic()
    with pytest.raises(a.ActivationError) as refused:
        with a.locked_activation(root, wait=1):
            pytest.fail("the activation ran beside a job holding the lock")
    assert 0.9 <= time.monotonic() - started < 10
    assert (
        str(refused.value)
        == "a running job or host operation still holds the lock after a 1 s wait"
    )
    assert a.lock_busy(str(refused.value))
    late = os.open(root / "state/activation.lock", os.O_RDONLY)
    _take_shared(root, late)  # no longer pending
    os.close(late)
    os.close(job)


def test_without_a_wait_a_running_job_refuses_at_once_and_is_named(tmp_path):
    root = _root(tmp_path)
    job = _hold(root / "state/activation.lock", fcntl.LOCK_SH)
    started = time.monotonic()
    with pytest.raises(a.ActivationError) as refused:
        with a.locked_activation(root):
            pytest.fail("the activation ran beside a job holding the lock")
    assert time.monotonic() - started < 5
    assert str(refused.value) == "a running job or host operation holds the lock"
    assert a.lock_busy(str(refused.value))
    os.close(job)


def test_a_host_with_no_marker_yet_lets_jobs_start(tmp_path):
    root = _root(tmp_path)
    assert not (root / "state/activation-pending.lock").exists()
    fd = os.open(root / "state/activation.lock", os.O_RDONLY)
    _take_shared(root, fd)
    os.close(fd)


def test_a_marker_that_is_not_a_regular_file_refuses_jobs(tmp_path):
    root = _root(tmp_path)
    os.mkfifo(root / "state/activation-pending.lock")
    fd = os.open(root / "state/activation.lock", os.O_RDONLY)
    with pytest.raises(a.ActivationError, match="replaced or redirected"):
        _take_shared(root, fd)
    os.close(fd)


def test_only_lock_refusals_read_as_a_busy_lock():
    assert a.lock_busy(a.ANOTHER_ACTIVATION)
    assert a.lock_busy("a running job or host operation holds the lock")
    assert not a.lock_busy("start repair requires a pending bots_started activation")
    assert not a.lock_busy("a running job or host operationXX")
