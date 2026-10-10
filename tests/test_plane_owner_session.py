"""Owner-only transport selection; runtime/browser ingress is not exercised."""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from claudlobby.plane.view import create_app
from claudlobby.plane.owner_access import SESSION_SECONDS
from tests.conftest import constructed_env
from tests.package_fixtures import source_package
from tests.test_plane_owner_browser import browser, _pair_locally, _post, _raw_http


def test_owner_transport_remains_protected_before_login_and_after_expiry(browser):
    _, client, store, _, clock = browser
    for path in ['/api-client.js', '/api-client.js?v=example', '/owner-api-client.js']:
        assert client.get(path).status_code == 403
    _pair_locally(client, store)
    assert _post(client, 'login').status_code == 200
    response = client.get('/api-client.js?v=example')
    assert response.status_code == 200
    assert 'mountSessionControls' in response.text and '/api/owner/status' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'].startswith('text/javascript')
    assert client.head('/api-client.js').status_code == 200
    assert client.get('/api-client.js', headers={'Host': 'other.example.test'}).status_code == 403
    assert client.post('/api-client.js').status_code == 405
    clock[0] += SESSION_SECONDS
    assert client.get('/api-client.js').status_code == 403


def test_default_plane_transport_has_no_owner_capability(browser):
    app, _, _, _, _ = browser
    client = TestClient(create_app(app.access.root, package=source_package()))
    transport = client.get('/api-client.js')
    assert transport.status_code == 200
    assert 'mountSessionControls' not in transport.text and '/api/owner/' not in transport.text
    page = client.get('/')
    assert 'id="owner-session"' in page.text and 'aria-label="Owner session" hidden' in page.text


def test_owner_transport_change_refreshes_the_import_url(browser, tmp_path, monkeypatch):
    from claudlobby.plane import view
    _, client, store, _, _ = browser
    _pair_locally(client, store)
    assert _post(client, 'login').status_code == 200
    ui = tmp_path / 'ui-assets'
    shutil.copytree(view.UI_DIR, ui)
    monkeypatch.setattr(view, 'UI_DIR', ui)
    before = client.get('/app.js').text
    transport = ui / 'owner-api-client.js'
    info = transport.stat()
    os.utime(transport, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    after = client.get('/app.js').text
    assert '/api-client.js?v=' in before and before != after


def test_session_controller_node_regressions():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_owner_session.test.mjs'))],
                            env=constructed_env(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('expired', [False, True])
def test_root_without_current_session_redirects_only_to_owner_entry(browser, method, expired):
    _, client, store, _, clock = browser
    if expired:
        _pair_locally(client, store)
        assert _post(client, 'login').status_code == 200
        clock[0] += SESSION_SECONDS
    response = client.request(method, '/?next=https://other.example.test', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/owner'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.content == b'' and 'set-cookie' not in response.headers
    for path in ['/api/tasks', '/api-client.js', '/index.html', '/app.js']:
        assert client.get(path, follow_redirects=False).status_code == 403
    # No automatic pairing/login is performed by the redirect or public shell.
    assert client.get('/owner').status_code == 200
    assert client.get('/api/tasks').status_code == 403


@pytest.mark.parametrize('headers', [{'Host': 'other.example.test'},
    {'Origin': 'https://other.example.test'}])
def test_root_boundary_refusal_is_never_redirected(browser, headers):
    _, client, _, _, _ = browser
    response = client.get('/', headers=headers, follow_redirects=False)
    assert response.status_code == 403 and 'location' not in response.headers


def test_root_unavailable_authority_is_not_a_signout_redirect(browser):
    _, client, store, _, _ = browser
    _pair_locally(client, store)
    assert _post(client, 'login').status_code == 200
    store.path.unlink()
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 503 and 'location' not in response.headers
    assert response.json()['state'] == 'unavailable'


def test_root_redirect_discards_refused_body_and_has_one_complete_response(browser, monkeypatch):
    app, _, _, _, _ = browser
    async def refused(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 403,
                    'headers': [(b'content-length', b'999'), (b'x-private', b'never-forward')]})
        await send({'type': 'http.response.body', 'body': b'private refused body', 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'more private bytes', 'more_body': False})
    monkeypatch.setattr(app, 'read_app', refused)
    messages = asyncio.run(_raw_http(app, '/', method='GET'))
    starts = [m for m in messages if m['type'] == 'http.response.start']
    bodies = [m for m in messages if m['type'] == 'http.response.body']
    assert len(starts) == len(bodies) == 1
    assert starts[0]['status'] == 303
    assert dict(starts[0]['headers'])[b'location'] == b'/owner'
    assert b'x-private' not in dict(starts[0]['headers'])
    assert bodies[0].get('body', b'') == b'' and not bodies[0].get('more_body', False)
