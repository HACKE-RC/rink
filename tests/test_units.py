"""Pure-function tests (no network)."""

import pytest

from rink import cli, links, render, upload, uploader, util
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
        self.current = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def add_task(self, description, total):
        task = len(self.current) + 1
        self.current[task] = {"description": description, "completed": 0, "total": total}
        return task

    def update(self, task, advance=None, completed=None, description=None):
        if advance is not None:
            self.current[task]["completed"] += advance
        if completed is not None:
            self.current[task]["completed"] = completed
        if description is not None:
            self.current[task]["description"] = description


def _progress_spies(monkeypatch):
    spies = []

    def factory():
        spy = ProgressSpy()
        spies.append(spy)
        return spy

    monkeypatch.setattr(upload, "progress_bar", factory)
    return spies


def _cfg():
    return Config("acct", "ak", "sk", "bucket")


def _sample_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    return f


def _sample_dir(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    return d


def _task(description, completed, total=5):
    return {"description": description, "completed": completed, "total": total}


def _successful_upload(client, bucket, src, key, progress=None, extra=None):
    if progress:
        progress(src.stat().st_size)


def _failing_upload(advance=0):
    def upload_file(client, bucket, src, key, progress=None, extra=None):
        if progress and advance:
            progress(advance)
        raise RuntimeError("boom")

    return upload_file


@pytest.mark.parametrize(
    "phase,target,outcome,expected",
    [
        ("upload", "a.txt", "active", "Uploading a.txt"),
        ("upload", "a.txt", "success", "✅ Uploaded a.txt"),
        ("upload", "a.txt", "failure", "❌ Upload failed a.txt"),
        ("zip", "d/", "active", "Zipping d/"),
        ("zip", "d/", "success", "✅ Zipped d/"),
        ("zip", "d/", "failure", "❌ Zip failed d/"),
    ],
)
def test_progress_description(phase, target, outcome, expected):
    assert render.progress_description(phase, target, outcome) == expected


def test_upload_one_finalizes_success(tmp_path, monkeypatch):
    f = _sample_file(tmp_path)
    spies = _progress_spies(monkeypatch)

    monkeypatch.setattr(uploader, "upload_file", _successful_upload)

    assert upload._upload_one(None, _cfg(), f, "a.txt", extra=None, quiet=False) == (
        "a.txt",
        5,
    )
    assert spies[0].current[1] == _task("✅ Uploaded a.txt", 5)


def test_upload_one_finalizes_failure_without_forcing_complete(tmp_path, monkeypatch):
    f = _sample_file(tmp_path)
    spies = _progress_spies(monkeypatch)

    monkeypatch.setattr(uploader, "upload_file", _failing_upload(advance=2))

    with pytest.raises(RuntimeError, match="boom"):
        upload._upload_one(None, _cfg(), f, "a.txt", extra=None, quiet=False)

    assert spies[0].current[1] == _task("❌ Upload failed a.txt", 2)


def test_recursive_progress_finalizes_success(tmp_path, monkeypatch):
    d = _sample_dir(tmp_path)
    spies = _progress_spies(monkeypatch)

    monkeypatch.setattr(uploader, "upload_file", _successful_upload)

    assert upload.upload_recursive(None, _cfg(), d, "", extra=None, quiet=False, workers=1) == [
        ("d/a.txt", 5)
    ]
    assert spies[0].current[1] == _task("✅ Uploaded d/", 5)


def test_recursive_progress_finalizes_failure_without_forcing_complete(tmp_path, monkeypatch):
    d = _sample_dir(tmp_path)
    spies = _progress_spies(monkeypatch)

    monkeypatch.setattr(uploader, "upload_file", _failing_upload(advance=2))

    assert upload.upload_recursive(None, _cfg(), d, "", extra=None, quiet=False, workers=1) == []
    assert spies[0].current[1] == _task("❌ Upload failed d/", 2)


def test_zip_progress_says_zipped_then_uploaded(tmp_path, monkeypatch):
    d = _sample_dir(tmp_path)
    spies = _progress_spies(monkeypatch)

    monkeypatch.setattr(uploader, "upload_file", _successful_upload)

    key, size = upload._upload_zip(None, _cfg(), d, None, "", extra=None, quiet=False)

    assert key == "d.zip"
    assert size > 0
    assert spies[0].current[1] == _task("✅ Zipped d/", 5)
    assert spies[1].current[1]["description"] == "✅ Uploaded d.zip"


def test_zip_progress_finalizes_failure_without_forcing_complete(tmp_path, monkeypatch):
    d = _sample_dir(tmp_path)
    spies = _progress_spies(monkeypatch)

    def fail_zip(folder, progress=None):
        if progress:
            progress(2)
        raise RuntimeError("boom")

    monkeypatch.setattr(uploader, "zip_folder", fail_zip)

    with pytest.raises(RuntimeError, match="boom"):
        upload._upload_zip(None, _cfg(), d, None, "", extra=None, quiet=False)

    assert spies[0].current[1] == _task("❌ Zip failed d/", 2)
