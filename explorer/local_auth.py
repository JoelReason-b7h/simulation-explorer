"""Tokens for the local stack without Cognito, so a run needs no network.

The services check a token's signature against the key set WireMock serves in the acceptance
profile, and `local_auth_patch.py` adds this module's public key to that set. A client token then
needs only a `client_id` that `platform_client_link` knows. A user token, the ops one, is looked up
with Cognito's GetUser; `AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER` sends that call here, and the
answer is read back out of the token, which is signed by this key alone.

The key is for the local stack only: no deployed environment trusts it.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from explorer import clock

KEY_PATH = Path(__file__).resolve().parent.parent / "local-auth" / "signing-key.pem"
KID = "simulation-explorer-local"
PORT = 8431
LIFETIME_SECONDS = 3600
OPS_USER = "7a1c0b52-5d4e-4a4f-9d1e-6f0c2b8e9a10"
OPS_ROLES = ["admin"]


def signing_key():
    return serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)


def generate_key():
    KEY_PATH.parent.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    KEY_PATH.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))


def public_jwk():
    numbers = signing_key().public_key().public_numbers()

    def b64(value):
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"alg": "RS256", "e": b64(numbers.e), "kid": KID, "kty": "RSA", "n": b64(numbers.n),
            "use": "sig"}


def lifetime():
    """An hour of real time. The services count it on the stack's clock, so under a fake clock at
    x10 a token that lived 3600 of its seconds would expire six real minutes into a run."""
    return int(clock.system_seconds(LIFETIME_SECONDS))


def _mint(claims):
    now = int(clock.time())
    body = {"iat": now, "exp": now + lifetime(), "jti": str(uuid.uuid4()),
            "token_use": "access", "iss": "simulation-explorer-local"}
    body.update(claims)
    return jwt.encode(body, signing_key(), algorithm="RS256", headers={"kid": KID})


def client_token(client_id, scope="public-api/default"):
    return _mint({"sub": client_id, "client_id": client_id, "scope": scope})


def ops_token(username=OPS_USER, roles=OPS_ROLES):
    return _mint({"sub": username, "username": username,
                  "custom:b7h:user_type": "OPERATIONS",
                  "custom:b7h:user_roles": json.dumps(roles),
                  "email": "{}@simulation-explorer.local".format(username)})


def _read(token):
    # PyJWT checks exp and iat against the host's real clock, which a fake-clock token fails, so
    # the expiry is checked here against the stack's clock.
    claims = jwt.decode(token, signing_key().public_key(), algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False})
    if claims.get("exp") is not None and claims["exp"] < clock.time():
        raise jwt.ExpiredSignatureError("the token expired at {}".format(claims["exp"]))
    return claims


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _answer(self, status, body, content_type="application/json"):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        target = self.headers.get("X-Amz-Target", "")
        if target.endswith(".GetUser"):
            return self._get_user(raw)
        if self.path.rstrip("/").endswith("/oauth2/token"):
            return self._client_credentials(raw)
        self._answer(404, {"message": "no route for {} {}".format(self.path, target)})

    def _client_credentials(self, raw):
        client_id = None
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            client_id = base64.b64decode(header[6:]).decode().split(":", 1)[0]
        form = dict(p.split("=", 1) for p in raw.split("&") if "=" in p)
        client_id = client_id or form.get("client_id")
        if not client_id:
            return self._answer(400, {"error": "invalid_client"})
        self._answer(200, {"access_token": client_token(client_id, form.get("scope",
                                                                           "public-api/default")
                                                        .replace("%2F", "/")),
                           "expires_in": lifetime(), "token_type": "Bearer"})

    def _get_user(self, raw):
        try:
            claims = _read(json.loads(raw)["AccessToken"])
        except Exception as error:  # noqa: BLE001 - any unreadable token is Cognito's NotAuthorized
            return self._answer(400, {"__type": "NotAuthorizedException", "message": str(error)},
                                "application/x-amz-json-1.1")
        attributes = [{"Name": k, "Value": v} for k, v in claims.items()
                      if k.startswith("custom:") or k == "email"]
        self._answer(200, {"Username": claims["username"], "UserAttributes": attributes},
                     "application/x-amz-json-1.1")


def serve(port=PORT):
    """Start the token and GetUser server on a daemon thread and return it."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def ensure_serving(port=PORT):
    """Start the server unless something already answers on the port, as a cycle's does."""
    try:
        return serve(port)
    except OSError:
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        generate_key()
        print(json.dumps(public_jwk()))
    else:
        ThreadingHTTPServer(("0.0.0.0", PORT), _Handler).serve_forever()
