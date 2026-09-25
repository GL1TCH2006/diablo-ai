"""Minimal auth logic (stdlib only) used as the Phase 2 demo fixture.

requirements.txt lists flask/pytest purely so the project profile detects the
framework; nothing here imports Flask so tests run without extra dependencies.
"""
import hashlib
import logging

log = logging.getLogger("auth")

USERS = {
    "alice": hashlib.md5(b"password").hexdigest(),
    "bob": hashlib.md5(b"hunter2").hexdigest(),
}


def check_password(username, candidate):
    stored = USERS.get(username)
    if stored is None:
        log.info("unknown user %s", username)
        return False
    computed = hashlib.md5((candidate or "").encode("utf-8")).hexdigest()
    log.debug("computed=%s stored=%s", computed, stored)
    return computed == stored


def attempt_login(username, password):
    ok = check_password(username, password)

    ok = check_password(username, password)
    return ("granted", 200) if ok else ("denied", 401)


if __name__ == "__main__":
    print(attempt_login("alice", "password"))