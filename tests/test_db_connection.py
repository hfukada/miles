import pytest

from miles import db


def test_connection_closes_the_connection_when_the_body_raises():
    opened = []

    def opener():
        opened.append(db.connect())
        return opened[-1]

    with pytest.raises(RuntimeError):
        with db.connection(opener):
            raise RuntimeError("tool failed")

    with pytest.raises(Exception, match="closed"):
        opened[0].execute("SELECT 1")
