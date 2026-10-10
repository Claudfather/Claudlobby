"""Canonical channel projection and real renderer; private synthetic data only."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.conftest import constructed_env
from tests.plane_setup import initialize_plane
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.view import _fetch_channel
from claudlobby.plane.db import connect_ro, db_file
from claudlobby.task_operations import nudge_body


def test_activity_renderer_with_canonical_nudge_projection(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for canonical activity rendering')
    initialize_plane(tmp_path)
    (tmp_path / 'state/plane/capture.json').write_text('{"*":"full"}')
    task, message = 'wi_' + '1' * 32, 'msg_' + '2' * 32
    reason, actor = 'Check\nfixture <status> — please.', 'human:reviewer'
    emit_batch(tmp_path, [
        {'event_type': 'work_item', 'emitter': 'fixture', 'fleet': 'example',
         'payload': {'work_item_id': task, 'title': 'Fixture task', 'created_by': actor}},
        {'event_type': 'task', 'emitter': 'claudlobby.tasks.v1', 'fleet': 'example',
         'payload': {'work_item_id': task, 'event': 'nudged', 'actor': actor, 'by': actor, 'reason': reason}},
        {'event_type': 'communication', 'emitter': 'claudlobby.tasks.v1', 'fleet': 'example',
         'payload': {'msg_id': message, 'sender': actor, 'recipient': 'bot:example/lead',
                     'message_class': 'task_request', 'command_type': 'query', 'work_item_id': task,
                     'body': nudge_body(task, None, actor, reason)}},
    ])
    with connect_ro(db_file(tmp_path)) as conn:
        thread = _fetch_channel(conn, {}, 10, 'example')['threads'][0]
    fixture = tmp_path / 'channel.json'
    fixture.write_text(json.dumps(thread))
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_activity_renderer.test.mjs'))],
                            env=constructed_env(PLANE_ACTIVITY_FIXTURE=str(fixture)),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
