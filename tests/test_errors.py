import pytest

from miles import errors


@pytest.mark.parametrize(
    "text",
    [
        "GET /x?access_token=abc123&per_page=2",
        "refresh_token=abc123",
        "client_secret='abc123'",
        "code=abc123",
        "key=abc123",
        "x-token=abc123",
    ],
)
def test_credential_pairs_are_redacted(text: str):
    assert "abc123" not in errors.redact(text)


@pytest.mark.parametrize("text", ["status_code=429", "monkey=1", "barcode=7", "hotkey=ctrl"])
def test_names_that_merely_end_in_a_credential_word_are_left_alone(text: str):
    assert errors.redact(text) == text


def test_describe_error_names_the_class_redacts_and_cuts_only_after_redacting():
    assert errors.describe_error(ValueError("bad access_token=abc123")) == "ValueError: bad access_token=[redacted]"

    text = errors.describe_error(RuntimeError("x" * 190 + " access_token=abcdefghijklmnop"))
    assert len(text) <= errors.MAX_LENGTH
    assert "abcdefghi" not in text
