"""Resolves environment settings and credentials.

Reads the same files as tools/performance-testing/run.py, in the same order, so one set of
credentials serves both tools. The auth directory is gitignored, so a git worktree will not
have it — point SIM_AUTH_DIR at the main checkout's copy in that case.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PERF_ROOT = Path(__file__).resolve().parents[2] / "performance-testing"
ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


class ConfigError(Exception):
    pass


def _parse(path):
    def resolve(match):
        value = os.environ.get(match.group(1))
        if value is not None:
            return value
        return match.group(2) if match.group(2) is not None else ""

    settings = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = ENV_VAR_PATTERN.sub(resolve, value.strip())
    return settings


E2E_AUTH_HELPER = (
    Path(__file__).resolve().parents[2]
    / "test-e2e/src/main/java/b7h/tools/e2e/auth/AuthenticationHelper.java"
)


def _e2e_pooled_credentials():
    """The committed non-prod credentials the acceptance suite uses for the pooled direct client.

    `direct-model-test-clientId` in core's application.yml names the same client, and non-prod
    platform creation links it to the newest direct platform, so this is the pair that reaches
    the local stack. Read rather than copied, so there is one place to change.
    """
    if not E2E_AUTH_HELPER.exists():
        return {}
    found = {}
    for line in E2E_AUTH_HELPER.read_text().splitlines():
        for constant, key in (("POOLED_CLIENT_ID", "auth_client_id"),
                              ("POOLED_CLIENT_SECRET", "auth_client_secret")):
            if " {} = \"".format(constant) in line:
                found[key] = line.split('"')[1]
    return found


def load(env_name="local"):
    envs = PERF_ROOT / "envs"
    base = envs / "base.env"
    named = envs / "{}.env".format(env_name)
    if not named.exists():
        raise ConfigError("no environment file at {}".format(named))

    settings = {}
    if base.exists():
        settings.update(_parse(base))
    settings.update(_parse(named))

    auth_dir = Path(os.environ.get("SIM_AUTH_DIR") or (PERF_ROOT / "data" / "auth"))
    auth_file = auth_dir / "{}.env".format(env_name)
    if auth_file.exists():
        settings.update(_parse(auth_file))

    # On local the direct client is not a free choice: core's `direct-model-test-clientId` is the
    # only client the database links to a direct platform, so any other pair gets a 401. It
    # therefore wins over an auth file, which is meant for the deployed environments.
    if env_name == "local":
        settings.update(_e2e_pooled_credentials())

    for key in list(settings):
        override = os.environ.get(key)
        if override is not None:
            settings[key] = override

    return settings


def require(settings, *keys):
    missing = [key for key in keys if not settings.get(key)]
    if missing:
        raise ConfigError(
            "missing settings: {}. Set them in the environment, or point SIM_AUTH_DIR at a "
            "directory holding <env>.env.".format(", ".join(missing))
        )
    return [settings[key] for key in keys]
