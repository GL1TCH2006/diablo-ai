def test_valid_login_grants():
    from app import attempt_login
    assert attempt_login("alice", "password") == ("granted", 200)


def test_invalid_password_denies():
    from app import attempt_login
    assert attempt_login("alice", "wrong") == ("denied", 401)


def test_unknown_user_denies():
    from app import attempt_login
    assert attempt_login("nobody", "password") == ("denied", 401)


def test_hash_input_never_grants():
    from app import check_password
    assert check_password("alice", hashlib_md5("password")) is False


def hashlib_md5(value):
    import hashlib
    return hashlib.md5(value.encode("utf-8")).hexdigest()