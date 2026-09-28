"""The timeouts in each Java service's configuration, as the environment variables that set them.

    python3 faketime/timeouts.py <sim-main checkout>     rewrite faketime/timeouts.json

libfaketime speeds up the monotonic clock too, so every timer in a service runs at the rate: at
x10 a 10-second HTTP read timeout gives up after one real second, and on the slower box the bank
simulator's credit notifications and clearing's statement polls timed out hundreds of times a
cycle. stack_patch.py multiplies each value here by the rate and sets it on the service, because
an environment variable beats application.yml.

The table is built from the services' application.yml and application-acceptance.yml, which only
a checkout with source has, so it is committed: the box's stack checkout has the compose files and
no source, and uses the committed table.

Taken: every read-, connect- and read-idle-timeout of Micronaut's global HTTP client and of each
named service client (the 10-second default where a client names none), and every placeholder
whose name holds "timeout" with a default: the database connection and statement timeouts, the
webhook outbox's call timeout, the Redis command timeout. Left alone: shutdown and executor
keep-alive timeouts and presigned URL lifetimes, which no real work waits on, and SQS long-poll
waits, which the acceptance profile sets to zero.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

# The compose service, and the module whose resources it reads.
SERVICES = {
    "adapter": "apps/savings-exchange/adapter",
    "clearing": "apps/savings-exchange/clearing",
    "compliance": "apps/forge/compliance",
    "compliance-api": "apps/savings-exchange/api/compliance-api",
    "core": "apps/savings-exchange/core",
    "core-ro": "apps/savings-exchange/core",
    "hot-sauce-bank": "apps/savings-exchange/fakes/hot-sauce-bank",
    "notification": "apps/forge/notification",
    "ops-api": "apps/savings-exchange/api/ops-api",
    "public-api": "apps/savings-exchange/api/public-api",
    "simulator-api": "apps/savings-exchange/api/simulator-api",
}
PROFILES = ("application.yml", "application-acceptance.yml")
CLIENT_KEYS = ("read-timeout", "connect-timeout", "read-idle-timeout")
DEFAULT_READ = "10s"
PLACEHOLDER = re.compile(r"\$\{([a-z0-9.\-]*timeout[a-z0-9.\-]*):`?([0-9]+(?:ms|s|m)?)`?\}")
SKIP = ("shutdown", "keepalive", "presigned")
LITERAL = re.compile(r"^`?([0-9]+(?:ms|s|m)?)`?$")


def env_name(key):
    return re.sub(r"[.\-]", "_", key).upper()


def _merge(base, over):
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _literal(value):
    match = LITERAL.match(str(value).strip())
    return match.group(1) if match else None


def _placeholder_default(value):
    match = PLACEHOLDER.search(str(value))
    return match.group(2) if match else None


def service_timeouts(module):
    texts = [(module / "src/main/resources" / p) for p in PROFILES]
    config, found = {}, {}
    for path in texts:
        if path.exists():
            _merge(config, yaml.safe_load(path.read_text()) or {})
    http = (config.get("micronaut") or {}).get("http") or {}
    client = http.get("client") or {}
    for key in CLIENT_KEYS:
        value = client.get(key)
        value = _literal(value) or _placeholder_default(value) if value is not None else None
        if value or key == "read-timeout":
            found[env_name("micronaut.http.client." + key)] = value or DEFAULT_READ
    for service, settings in (http.get("services") or {}).items():
        settings = settings or {}
        for key in CLIENT_KEYS:
            value = settings.get(key)
            value = _literal(value) or _placeholder_default(value) if value is not None else None
            if value or key == "read-timeout":
                found[env_name("micronaut.http.services.{}.{}".format(service, key))] = \
                    value or DEFAULT_READ
    for path in texts:
        if not path.exists():
            continue
        for name, default in PLACEHOLDER.findall(path.read_text()):
            if name.startswith("micronaut.") or any(s in name for s in SKIP):
                continue
            found.setdefault(env_name(name), default)
    return dict(sorted(found.items()))


def build(checkout):
    checkout = Path(checkout)
    return {service: service_timeouts(checkout / module) for service, module in SERVICES.items()
            if (checkout / module).exists()}


def scaled(value, rate):
    """'30s' at x10 is '300s'; a bare number is milliseconds or seconds as the service reads it,
    and scales the same way."""
    match = re.match(r"^([0-9]+)(ms|s|m)?$", value)
    number, unit = int(match.group(1)), match.group(2) or ""
    return "{}{}".format(int(round(number * rate)), unit)


if __name__ == "__main__":
    table = build(sys.argv[1])
    out = Path(__file__).resolve().parent / "timeouts.json"
    out.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    print("{} services, {} timeouts, in {}".format(
        len(table), sum(len(v) for v in table.values()), out))
