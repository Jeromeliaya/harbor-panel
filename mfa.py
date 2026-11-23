"""Second-factor storage and short-lived login/management flows for the single administrator."""

from base64 import b64encode
from collections import deque
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import hmac
from io import BytesIO
import json
import re
import secrets
import sqlite3
import time

import pyotp
import qrcode
from webauthn import (
    generate_authentication_options, generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url, parse_authentication_credential_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from files import PanelError


@dataclass
class Flow:
    purpose: str
    owner: str
    expires: float
    remaining: int
    origin: str
    data: object = None
    scope: str = "mfa"


class MFA:
    TTL = 300
    ATTEMPTS = 5
    MAX_FLOWS = 64
    MAX_PASSKEYS = 10

    def __init__(self, path):
        self.path = path
        self.flows = {}
        self.failures = deque()
        if path:
            with self._db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS mfa_totp (id INTEGER PRIMARY KEY CHECK(id=1), secret TEXT NOT NULL, last_step INTEGER NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS mfa_passkeys (credential_id BLOB PRIMARY KEY, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL, rp_id TEXT NOT NULL, created_at INTEGER NOT NULL)")

    @contextmanager
    def _db(self, write=False):
        with closing(sqlite3.connect(self.path)) as db, db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def status(self, rp_id):
        if not self.path:
            return {"totp": False, "passkeys": [], "methods": []}
        with self._db() as db:
            has_totp = db.execute("SELECT 1 FROM mfa_totp WHERE id=1").fetchone() is not None
            keys = db.execute("SELECT credential_id, rp_id, created_at FROM mfa_passkeys ORDER BY created_at, credential_id").fetchall()
        methods = (["passkey"] if rp_id and any(key[1] == rp_id for key in keys) else []) + (["totp"] if has_totp else [])
        return {"totp": has_totp, "passkeys": [{"id": bytes_to_base64url(key[0]), "current_rp": key[1] == rp_id, "created_at": key[2]} for key in keys], "methods": methods}

    def enabled(self):
        if not self.path:
            return False
        with self._db() as db:
            return bool(db.execute("SELECT EXISTS(SELECT 1 FROM mfa_totp) OR EXISTS(SELECT 1 FROM mfa_passkeys)").fetchone()[0])

    def clear(self):
        self.flows.clear()

    def clear_owner(self, owner):
        for token in [token for token, flow in self.flows.items() if flow.purpose != "login" and flow.owner == owner]:
            del self.flows[token]

    def begin(self, purpose, owner, origin, data=None, *, scope="mfa"):
        now = time.monotonic()
        self.flows = {token: flow for token, flow in self.flows.items() if flow.expires > now}
        if purpose != "login":
            for token in [token for token, flow in self.flows.items() if flow.owner == owner and flow.purpose == purpose]:
                del self.flows[token]
        if len(self.flows) >= self.MAX_FLOWS:
            del self.flows[next(iter(self.flows))]
        token = secrets.token_urlsafe(32)
        self.flows[token] = Flow(purpose, owner, now + self.TTL, self.ATTEMPTS, origin, data, scope)
        return token

    def get(self, token, purpose, owner, origin):
        flow = self.flows.get(token) if isinstance(token, str) and len(token) == 43 else None
        if not flow or flow.owner != owner or flow.purpose != purpose or flow.origin != origin:
            raise PanelError("Verification has expired. Start again.", 410, code="mfa_restart")
        if flow.expires <= time.monotonic() or flow.remaining <= 0:
            self.flows.pop(token, None)
            raise PanelError("Verification has expired. Start again.", 410, code="mfa_restart")
        return flow

    def take_grant(self, token, owner, origin, scope):
        flow = self.get(token, "grant", owner, origin)
        if flow.scope != scope:
            raise PanelError("Verification is not valid for this operation.", 403)
        del self.flows[token]

    @contextmanager
    def attempt(self, token, flow):
        now = time.monotonic()
        while self.failures and now - self.failures[0] >= 60:
            self.failures.popleft()
        if len(self.failures) >= 10:
            raise PanelError("Too many verification attempts. Try again in one minute.", 429)
        flow.remaining -= 1
        try:
            yield
        except PanelError:
            self.failures.append(now)
            if not flow.remaining:
                self.flows.pop(token, None)
                raise PanelError("Too many verification attempts. Start again.", 410, code="mfa_restart") from None
            raise
        else:
            self.flows.pop(token, None)

    def assertion_options(self, token, purpose, owner, origin, rp_id):
        flow = self.get(token, purpose, owner, origin)
        if not rp_id:
            raise PanelError("Passkeys require an HTTPS domain name.", 403)
        if flow.data is None:
            with self._db() as db:
                keys = db.execute("SELECT credential_id FROM mfa_passkeys WHERE rp_id=?", (rp_id,)).fetchall()
            if not keys:
                raise PanelError("No passkey is available at this domain.", 409)
            options = generate_authentication_options(
                rp_id=rp_id, allow_credentials=[PublicKeyCredentialDescriptor(id=key[0]) for key in keys],
                user_verification=UserVerificationRequirement.REQUIRED, timeout=120_000,
            )
            flow.data = (options.challenge, json.loads(options_to_json(options)))
        return flow.data[1]

    def verify(self, token, purpose, owner, origin, rp_id, user_handle, method, body):
        flow = self.get(token, purpose, owner, origin)
        with self.attempt(token, flow):
            if method == "totp" and self._use_totp(body.get("code")):
                return flow.scope
            if method == "passkey":
                challenge, flow.data = flow.data, None
                if challenge is not None and self._use_passkey(body.get("credential"), challenge[0], origin, rp_id, user_handle):
                    return flow.scope
            raise PanelError("Second factor was invalid, expired, or already used.", 403)

    @staticmethod
    def _match_totp(secret, code, last_step):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            return None
        step = int(time.time()) // 30
        totp = pyotp.TOTP(secret)
        # Prefer the newest matching step and never accept an earlier one afterward.
        for candidate in (step + 1, step, step - 1):
            if candidate > last_step and hmac.compare_digest(code, totp.generate_otp(candidate)):
                return candidate
        return None

    def _use_totp(self, code):
        with self._db(write=True) as db:
            row = db.execute("SELECT secret, last_step FROM mfa_totp WHERE id=1").fetchone()
            step = self._match_totp(row[0], code, row[1]) if row else None
            if step is None:
                return False
            db.execute("UPDATE mfa_totp SET last_step=? WHERE id=1", (step,))
            return True

    def _use_passkey(self, credential, challenge, origin, rp_id, user_handle):
        if not isinstance(credential, dict) or not rp_id:
            return False
        try:
            assertion = parse_authentication_credential_json(credential)
        except Exception:
            return False
        if assertion.response.user_handle is not None and assertion.response.user_handle != user_handle:
            return False
        with self._db(write=True) as db:
            row = db.execute("SELECT public_key, sign_count FROM mfa_passkeys WHERE credential_id=? AND rp_id=?", (assertion.raw_id, rp_id)).fetchone()
            if not row:
                return False
            try:
                verified = verify_authentication_response(
                    credential=assertion, expected_challenge=challenge, expected_rp_id=rp_id,
                    expected_origin=origin, credential_public_key=row[0], credential_current_sign_count=row[1],
                    require_user_verification=True,
                )
            except Exception:
                # Library exceptions can include parts of the untrusted credential.
                return False
            db.execute("UPDATE mfa_passkeys SET sign_count=? WHERE credential_id=?", (verified.new_sign_count, assertion.raw_id))
            return True

    def start_enrollment(self, grant, owner, origin, rp_id, username, user_handle, method):
        self.take_grant(grant, owner, origin, "mfa")
        status = self.status(rp_id)
        if method == "totp":
            if status["totp"]:
                raise PanelError("Google Authenticator is already enrolled.", 409)
            secret = pyotp.random_base32()
            uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Harbor Panel")
            image = BytesIO()
            qrcode.make(uri).save(image, format="PNG")
            token = self.begin("enroll", owner, origin, (method, secret))
            return {"enrollment_id": token, "secret": secret, "qr": "data:image/png;base64," + b64encode(image.getvalue()).decode("ascii")}
        if method == "passkey":
            if not rp_id:
                raise PanelError("Passkeys require an HTTPS domain name.", 403)
            if len(status["passkeys"]) >= self.MAX_PASSKEYS:
                raise PanelError("Remove a passkey before adding another.", 409)
            with self._db() as db:
                keys = db.execute("SELECT credential_id FROM mfa_passkeys WHERE rp_id=?", (rp_id,)).fetchall()
            options = generate_registration_options(
                rp_id=rp_id, rp_name="Harbor Panel", user_id=user_handle, user_name=username,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED,
                ),
                exclude_credentials=[PublicKeyCredentialDescriptor(id=key[0]) for key in keys], timeout=120_000,
            )
            token = self.begin("enroll", owner, origin, (method, options.challenge))
            return {"enrollment_id": token, "options": json.loads(options_to_json(options))}
        raise PanelError("Unknown second-factor method.")

    def finish_enrollment(self, token, owner, origin, rp_id, body):
        flow = self.get(token, "enroll", owner, origin)
        method, value = flow.data
        with self.attempt(token, flow):
            if method == "totp":
                step = self._match_totp(value, body.get("code"), -1)
                if step is None:
                    raise PanelError("Enter a valid six-digit code from the new authenticator.", 403)
                with self._db(write=True) as db:
                    db.execute("INSERT INTO mfa_totp VALUES (1, ?, ?)", (value, step))
            else:
                # An attestation challenge is consumed even when verification fails.
                self.flows.pop(token, None)
                credential = body.get("credential")
                if not isinstance(credential, dict):
                    raise PanelError("Passkey could not be verified. Start again.", 410, code="mfa_restart")
                try:
                    verified = verify_registration_response(
                        credential=credential, expected_challenge=value, expected_origin=origin,
                        expected_rp_id=rp_id, require_user_verification=True,
                    )
                except Exception:
                    raise PanelError("Passkey could not be verified. Start again.", 410, code="mfa_restart") from None
                try:
                    with self._db(write=True) as db:
                        db.execute("INSERT INTO mfa_passkeys VALUES (?, ?, ?, ?, ?)", (verified.credential_id, verified.credential_public_key, verified.sign_count, rp_id, int(time.time())))
                except sqlite3.IntegrityError:
                    raise PanelError("That passkey is already enrolled. Start again.", 409, code="mfa_restart") from None

    def remove(self, grant, owner, origin, method, credential_id):
        self.take_grant(grant, owner, origin, "mfa")
        with self._db(write=True) as db:
            if method == "totp":
                changed = db.execute("DELETE FROM mfa_totp WHERE id=1").rowcount
            elif method == "passkey" and isinstance(credential_id, str):
                keys = db.execute("SELECT credential_id FROM mfa_passkeys").fetchall()
                row = next((key for key in keys if bytes_to_base64url(key[0]) == credential_id), None)
                changed = db.execute("DELETE FROM mfa_passkeys WHERE credential_id=?", row).rowcount if row else 0
            else:
                raise PanelError("Unknown second factor.")
            if not changed:
                raise PanelError("Second factor was not found.", 404)


def webauthn_user_handle(salt):
    return hashlib.sha256(b"Harbor Panel WebAuthn user\x00" + salt).digest()
