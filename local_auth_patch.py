"""Point a checkout's local stack at the harness's own tokens, so a run needs no network.

    python3 local_auth_patch.py <stack checkout>

Run it before the stack launches; it is safe to run again. It changes files the compose stack
mounts or reads, never product code:

- adds the harness's public key to the JWKS WireMock serves for Cognito, so the services accept a
  token `explorer.local_auth` signs, beside the dev pool's keys they already accept;
- sends ops-api's, simulator-api's and compliance-api's Cognito GetUser call to the harness, whose
  answer comes from the token itself. Only those three read a user token, and the AWS SDK takes
  the endpoint from `AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER` without touching their other AWS
  clients.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from explorer import local_auth

JWKS = "docker/wiremock/__files/cognito-jwks.json"
USER_TOKEN_SERVICES = ("docker/ops.env", "docker/simulator-api.env", "docker/compliance-api.env")
ENDPOINT = "AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER=http://host.docker.internal:{}".format(
    local_auth.PORT)


def patch(checkout):
    """Returns True when a file changed, because a running stack read them all at start."""
    checkout = Path(checkout)
    if not local_auth.KEY_PATH.exists():
        local_auth.generate_key()
    changed = False

    jwks_path = checkout / JWKS
    before = jwks_path.read_text()
    jwks = json.loads(before)
    jwks["keys"] = [k for k in jwks["keys"] if k.get("kid") != local_auth.KID]
    jwks["keys"].append(local_auth.public_jwk())
    after = json.dumps(jwks, indent=2) + "\n"
    if after != before:
        jwks_path.write_text(after)
        changed = True

    for name in USER_TOKEN_SERVICES:
        env_path = checkout / name
        before = env_path.read_text()
        lines = [line for line in before.splitlines()
                 if not line.startswith("AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER=")]
        lines.append(ENDPOINT)
        after = "\n".join(lines) + "\n"
        if after != before:
            env_path.write_text(after)
            changed = True
    print("local auth {} in {}".format("patched" if changed else "already patched", checkout))
    return changed


if __name__ == "__main__":
    patch(sys.argv[1])
