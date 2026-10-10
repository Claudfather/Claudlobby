"""Canonical channel projection and real renderer; private synthetic data only."""
import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from tests.conftest import constructed_env
from tests.plane_fixtures import ro
from tests.test_task_operations import estate, _manager_route  # noqa: F401
from tests.test_task_operations import _feedback_human
from claudlobby.message_payload import MessageBody
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.view import _fetch_channel
from claudlobby import task_operations


@pytest.mark.parametrize('assigned', [False, True])
def test_activity_renderer_with_canonical_nudge_projection(estate, tmp_path, assigned):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for canonical activity rendering')
    ctx, _ = estate
    task = task_operations.admit(ctx, str(uuid4()), title='Fixture task')
    if assigned:
        task_operations.assign(ctx, str(uuid4()), task.task_id, bot_id='worker')
    task_operations.nudge(ctx, str(uuid4()), task.task_id,
                          reason='Check\nfixture <status> — please.', route=_manager_route(ctx))
    with ro(ctx.root) as conn:
        thread = _fetch_channel(conn, {}, 10, 'example')['threads'][0]
    fixture = tmp_path / 'channel.json'
    fixture.write_text(json.dumps(thread))
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_activity_renderer.test.mjs'))],
                            env=constructed_env(PLANE_ACTIVITY_FIXTURE=str(fixture)),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_activity_renderer_with_canonical_queued_feedback(estate, tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for canonical activity rendering')
    ctx, conn = estate
    task = task_operations.admit(ctx, str(uuid4()), title='Queued work')
    human = _feedback_human(ctx, conn)
    feedback = task_operations.feedback(human, str(uuid4()), task.task_id,
        body=MessageBody('Consider <this> next.'), expected_assignment_id=None,
        route=_manager_route(human))
    emit_batch(ctx.root, [{'event_type': 'transmission', 'emitter': 'synthetic-renderer',
        'event_id': 'ev_' + uuid4().hex, 'occurred_at': '2026-10-10T00:00:00Z',
        'fleet': 'example', 'payload': {'msg_id': feedback.message_id, 'attempt_no': 1,
            'carrier': 'tmux', 'destination': 'bot:example/manager', 'state': 'pane_submitted'}}])
    with ro(ctx.root) as reader:
        thread = _fetch_channel(reader, {}, 10, 'example')['threads'][0]
    assert thread['delivered'] and feedback.task.state == 'queued'
    assert feedback.task.current_assignment is None
    fixture = tmp_path / 'feedback-channel.json'
    fixture.write_text(json.dumps(thread))
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_activity_renderer.test.mjs'))],
        env=constructed_env(PLANE_FEEDBACK_FIXTURE=str(fixture)),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
