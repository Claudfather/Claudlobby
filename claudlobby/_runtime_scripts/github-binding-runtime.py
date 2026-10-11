#!/usr/bin/env python3
"""Strict named GitHub consumers. No cache, ambient identity, or redirect fallback.

The plan-owned binding file is reread for every mint. Tokens have GitHub's
residual lifetime after revocation; this door cannot revoke issued tokens.
Managed issuance requires an external HTTPS issuer that independently authorizes
this exact binding and revision. This file does not implement that service.
"""
import base64
import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

class Refused(ValueError):
    pass


def binding_document():
    path = os.environ.get('CLAUDLOBBY_GITHUB_BINDINGS')
    if not path:
        raise Refused('named GitHub binding file is required')
    with open(path, 'rb') as stream:
        content = stream.read()
    expected = os.environ.get('CLAUDLOBBY_GITHUB_BINDING_SHA256', '')
    if not expected or hashlib.sha256(content).hexdigest() != expected:
        raise Refused('named GitHub grant differs from the session-approved configuration')
    config = json.loads(content)
    if config.get('schema') != 1 or not isinstance(config.get('bindings'), list):
        raise Refused('invalid named GitHub binding file')
    return config


def bindings():
    return binding_document()['bindings']


def repo_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', value or ''):
        raise Refused('explicit owner/repo is required')
    if any(part in ('.', '..') for part in value.split('/')):
        raise Refused('ambiguous repository name')
    return value.removesuffix('.git').lower()


def binding(repo):
    repo = repo_name(repo)
    rows = [row for row in bindings() if row.get('repo') == repo]
    if len(rows) != 1:
        raise Refused('repository is unbound or ambiguous')
    row = rows[0]
    expected = os.environ.get('CLAUDLOBBY_GITHUB_REVISION')
    if expected and expected != row.get('revision'):
        raise Refused('named GitHub binding was revoked or changed')
    return row


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Refused('credential redirects are refused')


def post(url, token, payload):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                    headers={'Authorization': 'Bearer ' + token,
                                             'Accept': 'application/vnd.github+json',
                                             'Content-Type': 'application/json',
                                             'X-GitHub-Api-Version': '2022-11-28'})
    # No ambient proxy can redirect credential-bearing traffic.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    started = time.monotonic()
    with opener.open(request, timeout=15) as response:
        result = json.load(response)
        # TLS-authenticated provider time proves remaining lifetime despite
        # small host clock differences. Never accept this field from the body.
        try:
            result['_issuer_time'] = parsedate_to_datetime(response.headers['Date']).isoformat()
            result['_request_started'] = started
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Refused('issuer did not supply its response time')
        return result


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def validate_token(row, result):
    token = result.get('token')
    repos = result.get('repositories')
    if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
        raise Refused('issuer returned no valid token')
    if not isinstance(repos, list) or len(repos) != 1:
        raise Refused('issuer did not prove exact repository scope')
    repository = repos[0]
    if not isinstance(repository, dict) or str(repository.get('full_name', '')).lower() != row['repo'] or type(repository.get('id')) is not int or repository['id'] <= 0:
        raise Refused('issuer returned a different repository')
    actual = result.get('permissions')
    expected = row['permissions']
    if not isinstance(actual, dict):
        raise Refused('issuer did not prove permissions')
    # GitHub supplies mandatory metadata:read even when omitted from request.
    allowed = dict(expected)
    allowed.setdefault('metadata', 'read')
    if any(key not in allowed or value != allowed[key] for key, value in actual.items()):
        raise Refused('issuer returned excess permissions')
    if any(actual.get(key) != value for key, value in expected.items()):
        raise Refused('issuer returned different permissions')
    if row['mode'] == 'managed' and result.get('revision') != row['revision']:
        raise Refused('managed issuer returned a different revision')
    return token


def token_expiry(value, issuer_time, request_started):
    """One conservative deadline, including HTTP and subsequent IPC time."""
    try:
        expiry = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        issued = datetime.datetime.fromisoformat(issuer_time.replace('Z', '+00:00'))
        if expiry.tzinfo is None or issued.tzinfo is None:
            raise ValueError('invalid expiry')
        remaining = (expiry - issued).total_seconds()
        if not 0 < remaining <= 3600:
            raise ValueError('invalid expiry')
        deadline = request_started + remaining
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ValueError('expired during issuance')
        return deadline
    except (AttributeError, TypeError, ValueError):
        raise Refused('issuer did not prove bounded token expiry')


def mint(repo, structured=False):
    row = binding(repo)
    refs = row['credential_refs']
    if row['mode'] == 'independent':
        key = os.environ.get(refs['private_key_path_env'])
        if not key:
            raise Refused('connection private key reference is unavailable')
        now = int(time.time())
        message = encoded(b'{"alg":"RS256","typ":"JWT"}') + '.' + encoded(json.dumps(
            {'iat': now - 60, 'exp': now + 540, 'iss': str(row['app_id'])}, separators=(',', ':')).encode())
        signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', key, '-binary'],
                                input=message.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        jwt = message + '.' + encoded(signed.stdout)
        result = post('https://api.github.com/app/installations/' + str(row['installation_id']) + '/access_tokens', jwt,
                      {'repositories': [row['repo'].split('/')[1]], 'permissions': row['permissions']})
    elif row['mode'] == 'managed':
        url = os.environ.get(refs['issuer_url_env'], '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise Refused('managed HTTPS issuer is unavailable or invalid')
        authorization = os.environ.get(refs['issuer_token_env'], '')
        if not authorization:
            raise Refused('managed issuer authorization is unavailable')
        result = post(url, authorization, {'schema': 1, 'binding': row, 'revision': row['revision']})
    else:
        raise Refused('unsupported connection mode')
    # Also catch a config change while issuance was in flight.
    if binding(repo) != row:
        raise Refused('binding changed during issuance')
    token = validate_token(row, result)
    raw_expiry = result.get('expires_at')
    issuer_time = result.get('_issuer_time')
    deadline = token_expiry(raw_expiry, issuer_time, result.get('_request_started'))
    if structured:
        return {'token': token, 'expires_at': raw_expiry, 'expires_monotonic': deadline}
    return token


def clean_env():
    config = binding_document()
    env = os.environ.copy()
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_PAT', 'GITHUB_PERSONAL_ACCESS_TOKEN',
                 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST', 'GH_REPO', 'GH_DEBUG',
                 'GITHUB_APP_ID', 'GITHUB_APP_INSTALLATION_ID', 'GITHUB_APP_PRIVATE_KEY_PATH'):
        env.pop(name, None)
    for row in config['bindings']:
        for name in row['credential_refs'].values():
            env.pop(name, None)
    for name in config.get('credential_env_names', []):
        env.pop(name, None)
    return env


def gh(real, args):
    if not args or any(arg in ('--hostname', '--host') or arg.startswith(('--hostname=', '--host=')) for arg in args):
        raise Refused('unknown target or host override is refused')
    if args[0] == 'api':
        endpoints = [arg for arg in args[1:] if arg.startswith(('repos/', '/repos/', 'http:', 'https:'))]
        if len(endpoints) != 1 or not endpoints[0].lstrip('/').startswith('repos/'):
            raise Refused('API calls require one repository endpoint')
        if any(piece in ('.', '..') for piece in endpoints[0].split('/')) or '%' in endpoints[0] or '?' in endpoints[0] or '#' in endpoints[0]:
            raise Refused('ambiguous API endpoint is refused')
        endpoint = endpoints[0].lstrip('/')
        pieces = endpoint.split('/')
        repo = repo_name('/'.join(pieces[1:3]))
        # Unknown positional endpoints and redirects must not accompany the approved target.
        if args[1] != endpoints[0] or any(a in ('--paginate', '--preview', '--header', '-H') or a.startswith(('--header=', '-H', '--hostname', '--host')) for a in args):
            raise Refused('unsupported API target form')
    else:
        allowed = {'pr': {'list', 'view', 'create', 'edit', 'close', 'reopen', 'merge', 'checks', 'diff', 'comment', 'review', 'ready', 'status'},
                   'issue': {'list', 'view', 'create', 'edit', 'close', 'reopen', 'comment', 'status'},
                   'repo': {'view'}}
        if len(args) < 2 or args[0] not in allowed or args[1] not in allowed[args[0]]:
            raise Refused('account-wide or unknown gh operation is refused')
        repo_view = args[:2] == ['repo', 'view']
        repo = None
        # These values are content, not repository selectors. A PR body may
        # contain links without changing the destination of the operation.
        content_flags = {'--body', '-b', '--body-file', '-F', '--title', '-t',
                         '--template', '--jq', '-q', '--search', '-S', '--json', '--branch'}
        skip_value = False
        for i, arg in enumerate(args[2:], start=2):
            if skip_value:
                skip_value = False
                continue
            if arg in content_flags:
                skip_value = True
                continue
            if any(arg.startswith(flag + '=') for flag in content_flags if flag.startswith('--')):
                continue
            if arg in ('-R', '--repo'):
                if repo_view:
                    raise Refused('repo view uses an owner/repo argument, not --repo')
                repo = repo_name(args[i + 1])
            elif arg.startswith('--repo='):
                repo = repo_name(arg.split('=', 1)[1])
            elif arg.startswith('-R') and len(arg) > 2:
                repo = repo_name(arg[2:])
            elif '://' in arg or arg.startswith('git@'):
                raise Refused('full URL targets are refused')
            elif repo_view and not arg.startswith('-'):
                if repo is not None:
                    raise Refused('repo view requires one repository')
                repo = repo_name(arg)
        if repo is None:
            remote = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], text=True).strip()
            match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^?#]+)', remote)
            if not match:
                raise Refused('current repository has no clear GitHub origin')
            repo = repo_name(match[1])
            if repo_view:
                args.insert(2, repo)
            else:
                args += ['--repo', repo]
    token = mint(repo)
    env = clean_env()
    env['GH_TOKEN'] = token
    env['GH_HOST'] = 'github.com'
    os.execve(real, [real] + args, env)


def main():
    try:
        action = sys.argv[1]
        if action == 'git':
            context = dict(line.rstrip('\n').split('=', 1) for line in sys.stdin if '=' in line)
            if context.get('host') != 'github.com' or context.get('protocol') != 'https':
                raise Refused('GitHub requires HTTPS with an unambiguous host and port')
            print('username=x-access-token\npassword=' + mint(context.get('path', '')))
        elif action == 'mint':
            structured = '--json' in sys.argv[2:]
            repos = [arg for arg in sys.argv[2:] if arg != '--json']
            result = mint(repos[0] if repos else os.environ.get('CLAUDLOBBY_GITHUB_REPO', ''), structured=structured)
            print(json.dumps(result) if structured else result, end='')
        elif action == 'gh':
            gh(sys.argv[2], sys.argv[3:])
        else:
            raise Refused('unknown strict consumer')
        return 0
    except Exception as error:
        # Never print response bodies, credentials or issuer URLs.
        print('github-binding-runtime: refused (' + type(error).__name__ + ')' +
              (': ' + str(error) if isinstance(error, Refused) else ''), file=sys.stderr)
        if len(sys.argv) > 1 and sys.argv[1] == 'git':
            print('quit=1')
        return 1


if __name__ == '__main__':
    sys.exit(main())
