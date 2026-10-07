"""HTTP access to the Direct, ops and simulator surfaces.

Every call is recorded. A rejection is an observation the explorer keeps, not a failure, so
nothing here raises on a 4xx — only a transport error stops a run.

The OAuth client_credentials flow comes from tools/performance-testing/lib/auth.py rather than
being written again here, so both tools mint tokens the same way.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

PERF_ROOT = Path(__file__).resolve().parents[2] / "performance-testing"
if str(PERF_ROOT) not in sys.path:
    sys.path.insert(0, str(PERF_ROOT))

from lib.auth import ClientCredentialsAuth  # noqa: E402


class Call:
    def __init__(self, method, path, status, body, elapsed_ms, request_body=None, headers=None):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        self.elapsed_ms = elapsed_ms
        self.request_body = request_body
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status < 300

    def __repr__(self):
        return "<{} {} {}>".format(self.method, self.path, self.status)


# Every call slower than this, in the order made, until the trial that made them takes them. A
# trial that took 60 seconds recorded only its total, so which of its six calls stalled was lost.
SLOW_MS = 5000
SLOW_CALLS = []


def take_slow_calls():
    taken = list(SLOW_CALLS)
    del SLOW_CALLS[:]
    return taken


class _Client:
    def __init__(self, base_url, timeout):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)
        self.calls = []

    def _headers(self):
        raise NotImplementedError

    # A transport fault answers with this status. It is outside the HTTP range on purpose, so
    # nothing mistakes it for something the service said.
    TRANSPORT_FAULT = 598

    def call(self, method, path, json_body=None, params=None, headers=None):
        started = time.time()
        try:
            response = self.http.request(
                method, self.base_url + path, json=json_body, params=params,
                headers=dict(self._headers(), **(headers or {}))
            )
        except httpx.HTTPError as fault:
            # A timeout or a dropped connection is something to record, not something to stop on.
            # A read timeout on one GET ended a whole run at trial 15, and a service that stops
            # answering is a finding the run should carry rather than die of.
            elapsed = (time.time() - started) * 1000
            record = Call(method, path, self.TRANSPORT_FAULT,
                          {"message": "{}: {}".format(type(fault).__name__, fault)},
                          elapsed, json_body)
            self.calls.append(record)
            SLOW_CALLS.append([method, path, record.status, round(elapsed)]) if elapsed >= SLOW_MS else None
            return record
        elapsed = (time.time() - started) * 1000
        try:
            body = response.json()
        except ValueError:
            body = response.text or None

        record = Call(method, path, response.status_code, body, elapsed, json_body,
                      {k.lower(): v for k, v in response.headers.items()})
        if elapsed >= SLOW_MS:
            SLOW_CALLS.append([method, path, response.status_code, round(elapsed)])
        self.calls.append(record)
        return record

    def close(self):
        self.http.close()


class DirectClient(_Client):
    """Talks to one platform, through the OAuth client bound to that platform."""

    def __init__(self, base_url, token_url, client_id, client_secret, scope=None, timeout=30.0):
        _Client.__init__(self, base_url, timeout)
        self.auth = ClientCredentialsAuth(token_url, client_id, client_secret, scope)

    def _headers(self):
        return {"Authorization": "Bearer {}".format(self.auth.get_token())}


class BearerClient(_Client):
    """Ops and simulator surfaces, which take a bearer token directly."""

    def __init__(self, base_url, token=None, timeout=60.0, renew=None):
        _Client.__init__(self, base_url, timeout)
        self.token = token
        self.renew = renew

    def _headers(self):
        return {"Authorization": "Bearer {}".format(self.token)} if self.token else {}

    def call(self, method, path, json_body=None, params=None, headers=None):
        result = _Client.call(self, method, path, json_body, params, headers)
        if getattr(result, "status", 0) != 401 or self.renew is None:
            return result
        # A token minted at startup outlives a short run and not a long one, so mint another and
        # try once more rather than reporting the ops endpoint as broken.
        self.token = self.renew()
        return _Client.call(self, method, path, json_body, params, headers)
