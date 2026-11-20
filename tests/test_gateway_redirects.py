"""No gateway call follows a redirect, so the Basic credentials never go anywhere else. Ours.

urllib's default redirect handler copies a request's headers onto the redirected request,
`Authorization` included and whatever the new host. Every call this scanner makes to the
gateway carries Basic credentials, so all three go through one opener whose redirect
handler refuses instead: the context fetch, the source-records fetch and the event POST.

Driven through the runner's REAL opener chain, with one in-process handler answering
`http_open` before urllib's own, so no socket is ever opened.
"""

from __future__ import annotations

import email.message
import io
import json
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError
from urllib.response import addinfourl

import pytest

from aeo import runner
from aeo.phases.source_records import SourceRecordsError


class _Gateway(urlrequest.BaseHandler):
    """Answers every http request in-process: first with `status`, and records each one."""

    handler_order = 100  # ahead of urllib's HTTPHandler (500), so it answers first

    def __init__(self, status: int, location: str | None, body: bytes = b"{}") -> None:
        self.status, self.location, self.body = status, location, body
        self.seen: list[tuple[str, str | None]] = []

    def http_open(self, req: Any) -> Any:
        self.seen.append((req.full_url, req.get_header("Authorization")))
        headers = email.message.Message()
        headers["Content-Type"] = "application/json"
        if self.location:
            headers["Location"] = self.location
        resp = addinfourl(io.BytesIO(self.body), headers, req.full_url, self.status)
        resp.msg = "Redirect" if 300 <= self.status < 400 else "OK"
        return resp


def _install(monkeypatch: Any, status: int, location: str | None = "http://elsewhere.invalid/x") -> _Gateway:
    monkeypatch.setenv("CI_USER", "scanner")
    monkeypatch.setenv("CI_PASSWORD", "s3cret")
    gateway = _Gateway(status, location)
    opener = runner._gateway_opener()
    opener.add_handler(gateway)
    monkeypatch.setattr(runner, "_GATEWAY_OPENER", opener)
    return gateway


def _only_the_gateway_was_asked(gateway: _Gateway) -> None:
    assert len(gateway.seen) == 1, gateway.seen
    url, auth = gateway.seen[0]
    assert url.startswith("http://gateway.invalid/")
    assert auth and auth.startswith("Basic ")
    assert not any("elsewhere" in u for u, _ in gateway.seen)


REDIRECTS = [301, 302, 303, 307, 308]


class TestTheOpenerItself:
    def test_the_shared_opener_has_no_handler_that_follows(self):
        handlers = runner._GATEWAY_OPENER.handlers
        redirectors = [h for h in handlers if isinstance(h, urlrequest.HTTPRedirectHandler)]
        assert redirectors, "the opener must carry the refusing handler, not none at all"
        assert all(isinstance(h, runner._NeverRedirect) for h in redirectors)

    def test_a_plain_answer_still_comes_through(self, monkeypatch):
        gateway = _install(monkeypatch, 200, None)
        gateway.body = json.dumps({"feed": "health_inspections", "items": []}).encode()
        assert runner.fetch_source_records("http://gateway.invalid", "o", "s", "inspections") == {
            "feed": "health_inspections", "items": [],
        }


class TestEveryGatewayCallRefusesARedirect:
    @pytest.mark.parametrize("status", REDIRECTS)
    def test_the_source_records_fetch(self, monkeypatch, status):
        gateway = _install(monkeypatch, status)
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://gateway.invalid", "o", "s", "inspections")
        assert exc.value.reason == "source_records_redirected"
        assert "elsewhere.invalid" in str(exc.value)
        _only_the_gateway_was_asked(gateway)

    @pytest.mark.parametrize("status", REDIRECTS)
    def test_the_context_fetch(self, monkeypatch, status):
        gateway = _install(monkeypatch, status)
        with pytest.raises(runner.GatewayRedirectRefused) as exc:
            runner.fetch_context("http://gateway.invalid", "o", "slug")
        assert exc.value.code == status
        assert "redirect" in str(exc.value) and "elsewhere.invalid" in str(exc.value)
        # Still an HTTPError, so `main` reports it as a failed context fetch.
        assert isinstance(exc.value, HTTPError)
        _only_the_gateway_was_asked(gateway)

    @pytest.mark.parametrize("status", REDIRECTS)
    def test_the_event_post(self, monkeypatch, status):
        gateway = _install(monkeypatch, status)
        with pytest.raises(runner.GatewayRedirectRefused):
            runner.post_event(
                "http://gateway.invalid", "run-1", "prospects", {"data": []},
                tenant_id="t", organization_id="o",
            )
        _only_the_gateway_was_asked(gateway)

    def test_a_redirect_with_no_location_is_reported_as_a_redirect(self, monkeypatch):
        # A negative case, NOT a proof of the fix: with no Location, urllib's own handler
        # follows nothing either, so this passes without `_NeverRedirect`. It pins only
        # that such an answer is still reported as `source_records_redirected`.
        gateway = _install(monkeypatch, 302, None)
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://gateway.invalid", "o", "s", "inspections")
        assert exc.value.reason == "source_records_redirected"
        assert len(gateway.seen) == 1
