"""Pure-function tests (no network)."""

import pytest

from rink import links, uploader, util
from rink.config import Config, ConfigError, parse_duration


@pytest.mark.parametrize(
    "text,secs",
    [
        ("30m", 1800),
        ("2h", 7200),
        ("7d", 604800),
        ("1w", 604800),
        ("90s", 90),
        ("3600", 3600),
        ("1h30m", 5400),
        ("45", 45),
    ],
)
def test_parse_duration_ok(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("bad", ["2x", "", "abc", "7dd", "1h foo"])
def test_parse_duration_bad(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_build_key():
    assert uploader.build_key("", "a.txt") == "a.txt"
    assert uploader.build_key("/backups/", "/a.txt") == "backups/a.txt"


def test_content_type():
    assert links.guess_content_type("x.pdf") == "application/pdf"
    assert links.guess_content_type("x.weirdext") == "application/octet-stream"


def test_public_url_encodes_spaces():
    cfg = Config("a", "b", "c", "d", public_base_url="https://pub-x.r2.dev")
    assert links.public_url(cfg, "dir/my file.txt") == (
        "https://pub-x.r2.dev/dir/my%20file.txt"
    )


def test_public_url_requires_base():
    with pytest.raises(ConfigError):
        links.public_url(Config("a", "b", "c", "d"), "k")


def test_random_token():
    t = util.random_token(4)
    assert len(t) == 8
    assert all(c in "0123456789abcdef" for c in t)
