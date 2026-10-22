"""creds-check: a provider-side failure is never a failed credential (#2424).

On 2026-10-21, inside Railway's outage, both declared Railway tokens answered
"Not Authorized" in a 200 body: the answer a revoked token gets, so the probe
alone cannot tell the two apart. These tests drive the real script through the
shared curl stub (tests/test_creds_check_telegram.py) and status pages
captured live that day (tests/fixtures/provider_status.py):

- a rejection is ``fail`` unless the provider's status page reports an open
  incident on the API the probe uses; then it is ``unknown``, reason
  ``provider_degraded``;
- no answer at all (a timeout, a 429, a 5xx) is ``unknown``, reason
  ``no_answer``;
- a status page that cannot be read is no evidence, so the rejection stays
  ``fail``;
- an unknown is no verdict: the run of the verdict before it carries on.
"""

import json

import pytest

from tests.fixtures.provider_status import PAGES, serve_status
from tests.test_creds_check_github_app import APP
from tests.test_creds_check_github_app import _fleet as _app_fleet
from tests.test_creds_check_github_app import _run as _app_run
from tests.test_creds_check_persistence import _age, _controlled, _posts
from tests.test_creds_check_telegram import (
    ANSWERS_503,
    IS_VALID,
    REJECTED_RAILWAY,
    TIMES_OUT,
    VALID_TOKEN,
    _run,
)

KEY = "railway_personal_token"
RAILWAY_OK = "railway-token" + IS_VALID


def _one_bot(token: str = VALID_TOKEN) -> list[tuple]:
    """A roster of one channel bot, for a fleet whose Telegram rows no test reads."""
    return [("bot1", "bot_one_bot", "T_BOT1_TOKEN", token)]


def _set(f: dict, token: str, page: str | None) -> dict:
    f["env"]["RAILWAY_PERSONAL_TOKEN"] = token
    serve_status(f["bindir"], "railway", page)
    return f


def _railway(tmp_path, token: str, page: str | None) -> dict:
    """A fleet whose one Railway token gets ``token``'s answer, and whose
    status page is ``page`` (None: the page cannot be reached)."""
    return _set(_controlled(tmp_path, railway_fails=False, roster=_one_bot()), token, page)


def _tick(f: dict, token: str, page: str | None) -> dict:
    return _run(_set(f, token, page))[KEY]


def _posts_about(f: dict, key: str) -> list[str]:
    return [line for line in _posts(f).splitlines() if key in line]


def _github_pat(tmp_path, token: str, page: str | None, *, body: str | None = None) -> dict:
    f = _controlled(tmp_path, railway_fails=False, roster=_one_bot())
    f["env"]["GITHUB_PAT"] = token
    serve_status(f["bindir"], "github", page, body=body)
    return _run(f)["github_pat"]


class TestARailwayRejection:
    def test_during_an_api_incident_reads_unknown_not_fail(self, tmp_path):
        f = _railway(tmp_path, REJECTED_RAILWAY, "railway-api-incident")
        row = _run(f)[KEY]
        assert row["status"] == "unknown"
        assert row["reason"] == "provider_degraded"
        assert "Not Authorized" in row["detail"]
        assert "Railway Platform Outage" in row["detail"]
        assert _posts_about(f, KEY) == []

    def test_with_no_open_incident_still_fails(self, tmp_path):
        f = _railway(tmp_path, REJECTED_RAILWAY, "railway-operational")
        row = _run(f)[KEY]
        assert row["status"] == "fail"
        assert "Not Authorized" in row["detail"]
        posts = _posts_about(f, KEY)
        assert len(posts) == 1 and f"{KEY} FAIL" in posts[0]

    def test_an_open_incident_off_the_api_does_not_soften_it(self, tmp_path):
        f = _railway(tmp_path, REJECTED_RAILWAY, "railway-builds-incident")
        assert _run(f)[KEY]["status"] == "fail"

    @pytest.mark.parametrize(
        "body",
        [
            None,
            # What the status site answers for a path it does not know.
            "<!DOCTYPE html><html><head><title>Railway Status</title></head><body></body></html>",
            # JSON that no longer has the captured shape.
            '{"incidents": [{"title": "Railway Platform Outage", "status": "IDENTIFIED"}]}',
        ],
        ids=["unreachable", "html", "another-shape"],
    )
    def test_a_status_page_it_cannot_read_does_not_hide_it(self, tmp_path, body):
        f = _railway(tmp_path, REJECTED_RAILWAY, None)
        if body is not None:
            serve_status(f["bindir"], "railway", body=body)
        assert _run(f)[KEY]["status"] == "fail"

    def test_a_working_token_reads_ok_during_an_incident(self, tmp_path):
        f = _railway(tmp_path, RAILWAY_OK, "railway-api-incident")
        assert _run(f)[KEY]["status"] == "ok"


class TestAGitHubRejection:
    def test_during_an_api_incident_reads_unknown(self, tmp_path):
        row = _github_pat(tmp_path, "ghp-token-is-rejected", "github-api-incident")
        assert row["status"] == "unknown"
        assert row["reason"] == "provider_degraded"
        assert "HTTP 401" in row["detail"]

    def test_with_github_operational_still_fails(self, tmp_path):
        row = _github_pat(tmp_path, "ghp-token-is-rejected", "github-operational")
        assert row["status"] == "fail"
        assert "HTTP 401 on /user" in row["detail"]

    @pytest.mark.parametrize("word", ["normal", None])
    def test_an_api_status_word_it_does_not_know_does_not_hide_it(self, tmp_path, word):
        summary = json.loads((PAGES / "github-operational.json").read_text())
        api = next(c for c in summary["components"] if c["name"] == "API Requests")
        if word is None:
            del api["status"]
        else:
            api["status"] = word
        row = _github_pat(tmp_path, "ghp-token-is-rejected", None, body=json.dumps(summary))
        assert row["status"] == "fail"

    def test_a_failed_app_mint_during_an_api_incident_reads_unknown(self, tmp_path):
        f = _app_fleet(tmp_path, app_env=APP, mint="fail")
        serve_status(f["bindir"], "github", "github-api-incident")
        row = _app_run(f)["github_pat"]
        assert row["status"] == "unknown"
        assert row["reason"] == "provider_degraded"
        assert "mint failed" in row["detail"]


class TestNoAnswer:
    @pytest.mark.parametrize("ending", [ANSWERS_503, TIMES_OUT, "-answers-html-200", "-answers-null-data"])
    def test_from_railway_reads_unknown_not_fail(self, tmp_path, ending):
        f = _railway(tmp_path, "railway-token" + ending, "railway-operational")
        row = _run(f)[KEY]
        assert row["status"] == "unknown"
        assert row["reason"] == "no_answer"
        assert _posts_about(f, KEY) == []

    @pytest.mark.parametrize("ending", [ANSWERS_503, TIMES_OUT])
    def test_from_github_reads_unknown_not_fail(self, tmp_path, ending):
        row = _github_pat(tmp_path, "ghp-token" + ending, "github-operational")
        assert row["status"] == "unknown"
        assert row["reason"] == "no_answer"

    def test_from_the_github_app_probe_reads_unknown_not_fail(self, tmp_path):
        row = _app_run(_app_fleet(tmp_path, app_env=APP, probe_code="503"))["github_pat"]
        assert row["status"] == "unknown"
        assert row["reason"] == "no_answer"

    @pytest.mark.parametrize("ending", [TIMES_OUT, "-answers-429", "-answers-500", "-answers-html-502"])
    def test_from_telegram_reads_unknown_not_fail(self, tmp_path, ending):
        f = _controlled(tmp_path, railway_fails=False, roster=_one_bot("444444:tg-token" + ending))
        row = _run(f)["telegram_f_bot1"]
        assert row["status"] == "unknown"
        assert row["reason"] == "no_answer"


class TestAnUnknownIsNoVerdict:
    """The run of the verdict before an unknown carries on through it."""

    def test_a_fail_keeps_its_age_through_an_outage(self, tmp_path):
        f = _railway(tmp_path, REJECTED_RAILWAY, "railway-operational")
        assert _run(f)[KEY]["status"] == "fail"
        _age(f, KEY, 5)
        since = json.loads(f["state"].read_text())[KEY]["since_epoch"]
        assert _tick(f, REJECTED_RAILWAY, "railway-api-incident")["status"] == "unknown"
        before = len(_posts_about(f, KEY))
        row = _tick(f, REJECTED_RAILWAY, "railway-operational")
        assert row["status"] == "fail"
        assert row["since_epoch"] == since
        new = _posts_about(f, KEY)[before:]
        assert len(new) == 1 and "STILL FAILING after 5d" in new[0]

    def test_a_fail_that_recovers_during_an_outage_reads_recovered(self, tmp_path):
        f = _railway(tmp_path, REJECTED_RAILWAY, "railway-operational")
        assert _run(f)[KEY]["status"] == "fail"
        assert _tick(f, REJECTED_RAILWAY, "railway-api-incident")["status"] == "unknown"
        before = len(_posts_about(f, KEY))
        assert _tick(f, RAILWAY_OK, "railway-operational")["status"] == "ok"
        new = _posts_about(f, KEY)[before:]
        assert len(new) == 1 and f"{KEY} RECOVERED" in new[0]

    def test_a_token_still_refused_once_the_incident_closes_alerts_as_a_failure(self, tmp_path):
        f = _railway(tmp_path, RAILWAY_OK, "railway-operational")
        assert _run(f)[KEY]["status"] == "ok"
        assert _tick(f, REJECTED_RAILWAY, "railway-api-incident")["status"] == "unknown"
        assert _posts_about(f, KEY) == []
        assert _tick(f, REJECTED_RAILWAY, "railway-operational")["status"] == "fail"
        posts = _posts_about(f, KEY)
        assert len(posts) == 1 and f"{KEY} FAIL" in posts[0]

    def test_an_unknown_that_outlasts_a_day_resurfaces_with_its_age(self, tmp_path):
        f = _railway(tmp_path, "railway-token" + ANSWERS_503, "railway-operational")
        assert _run(f)[KEY]["status"] == "unknown"
        _run(f)
        assert _posts_about(f, KEY) == []
        _age(f, KEY, 1)
        assert _run(f)[KEY]["status"] == "unknown"
        posts = _posts_about(f, KEY)
        assert len(posts) == 1 and "1d" in posts[0] and "FAIL" not in posts[0]
