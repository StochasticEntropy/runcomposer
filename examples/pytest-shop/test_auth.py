"""Auth checks of the fictional web shop (pytest example corpus)."""


def test_login():
    assert "session" in {"session": "abc"}


def test_logout():
    session = {"session": "abc"}
    session.clear()
    assert not session
