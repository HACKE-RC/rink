"""Pure-function tests (no network)."""

import pytest

from rink import cli, links, upload, uploader, util
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


@pytest.mark.parametrize(
    "n,expected",
    [(0, "0B"), (512, "512B"), (1024, "1.0KB"), (1536, "1.5KB"),
     (1048576, "1.0MB"), (1024**4, "1.0TB"), (1024**5, "1.0PB")],
)
def test_human_sizes(n, expected):
    assert cli._human(n) == expected


@pytest.mark.parametrize(
    "secs,expected",
    [(0, "<1m"), (30, "<1m"), (90, "1m"), (3600, "1h"), (90061, "1d 1h")],
)
def test_human_duration(secs, expected):
    assert cli._human_duration(secs) == expected


def test_is_expired():
    assert cli._is_expired(None) is False           # permanent
    assert cli._is_expired(100, now=50) is False    # future
    assert cli._is_expired(100, now=100) is True    # boundary == now
    assert cli._is_expired(100, now=150) is True    # past


class ProgressSpy:
    def __init__(self):
        self.descriptions = []
        self.current = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def add_task(self, description, total):
        self.descriptions.append(description)
        task = len(self.descriptions)
        self.current[task] = {"description": description, "completed": 0, "total": total}
        return task

    def update(self, task, advance=None, completed=None, description=None):
        if advance is not None:
            self.current[task]["completed"] += advance
        if completed is not None:
            self.current[task]["completed"] = completed
        if description is not None:
            self.current[task]["description"] = description


def test_upload_one_progress_says_uploading(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    cfg = Config("acct", "ak", "sk", "bucket")
    spy = ProgressSpy()

    monkeypatch.setattr(upload, "progress_bar", lambda: spy)
    monkeypatch.setattr(
        uploader,
        "upload_file",
        lambda client, bucket, src, key, progress=None, extra=None: progress
        and progress(src.stat().st_size),
    )

    assert upload._upload_one(None, cfg, f, "a.txt", extra=None, quiet=False) == ("a.txt", 5)
    assert spy.descriptions == ["Uploading a.txt"]
    assert spy.current[1] == {"description": "✅ Uploaded a.txt", "completed": 5, "total": 5}


def test_recursive_progress_says_uploading_folder(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    cfg = Config("acct", "ak", "sk", "bucket")
    spy = ProgressSpy()

    monkeypatch.setattr(upload, "progress_bar", lambda: spy)
    monkeypatch.setattr(
        uploader,
        "upload_file",
        lambda client, bucket, src, key, progress=None, extra=None: progress
        and progress(src.stat().st_size),
    )

    assert upload.upload_recursive(None, cfg, d, "", extra=None, quiet=False, workers=1) == [
        ("d/a.txt", 5)
    ]
    assert spy.descriptions == ["Uploading d/"]
    assert spy.current[1] == {"description": "✅ Uploaded d/", "completed": 5, "total": 5}


def test_recursive_progress_marks_failed_upload(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    cfg = Config("acct", "ak", "sk", "bucket")
    spy = ProgressSpy()

    monkeypatch.setattr(upload, "progress_bar", lambda: spy)

    def fail_upload(client, bucket, src, key, progress=None, extra=None):
        if progress:
            progress(src.stat().st_size)
        raise RuntimeError("boom")

    monkeypatch.setattr(uploader, "upload_file", fail_upload)

    assert upload.upload_recursive(None, cfg, d, "", extra=None, quiet=False, workers=1) == []
    assert spy.descriptions == ["Uploading d/"]
    assert spy.current[1] == {
        "description": "❌ Upload failed d/",
        "completed": 5,
        "total": 5,
    }


def test_zip_progress_says_zipped_then_uploaded(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    cfg = Config("acct", "ak", "sk", "bucket")
    spies = []

    def progress_factory():
        spy = ProgressSpy()
        spies.append(spy)
        return spy

    monkeypatch.setattr(upload, "progress_bar", progress_factory)
    monkeypatch.setattr(
        uploader,
        "upload_file",
        lambda client, bucket, src, key, progress=None, extra=None: progress
        and progress(src.stat().st_size),
    )

    key, size = upload._upload_zip(None, cfg, d, None, "", extra=None, quiet=False)

    assert key == "d.zip"
    assert size > 0
    assert spies[0].descriptions == ["Zipping d/"]
    assert spies[0].current[1] == {"description": "✅ Zipped d/", "completed": 5, "total": 5}
    assert spies[1].descriptions == ["Uploading d.zip"]
    assert spies[1].current[1]["description"] == "✅ Uploaded d.zip"
