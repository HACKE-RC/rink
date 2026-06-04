"""End-to-end CLI tests driving real commands against a mocked S3 (moto)."""

import json
import time

import boto3
import pytest
from botocore.config import Config as BotoConfig
from moto import mock_aws
from typer.testing import CliRunner

from rink import cli, config as cfgmod, db, uploader

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Isolate config: no real file, credentials via env.
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "nope.toml")
    monkeypatch.setenv("RINK_ACCOUNT_ID", "acct")
    monkeypatch.setenv("RINK_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("RINK_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("RINK_BUCKET", "test")
    monkeypatch.setenv("RINK_PUBLIC_BASE_URL", "https://pub-x.r2.dev")
    # Isolate the SQLite log.
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rink.db")

    with mock_aws():
        client = boto3.client(
            "s3", region_name="us-east-1", config=BotoConfig(signature_version="s3v4")
        )
        client.create_bucket(Bucket="test")
        monkeypatch.setattr(uploader, "make_client", lambda cfg: client)
        yield client


def _file(tmp_path, name="a.txt", text="hello"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_up_file_then_ls(env, tmp_path):
    f = _file(tmp_path)
    r = runner.invoke(cli.app, ["up", str(f), "--quiet", "--expiry", "1h"])
    assert r.exit_code == 0
    assert "X-Amz-Signature" in r.stdout

    r2 = runner.invoke(cli.app, ["ls"])
    assert r2.exit_code == 0
    # The a.txt row must be tracked (not "untracked") — check that row, not the legend.
    row = next(line for line in r2.stdout.splitlines() if "a.txt" in line)
    assert "untracked" not in row


def test_up_recursive_records_every_file(env, tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "p.txt").write_text("1")
    (d / "sub" / "q.txt").write_text("2")

    r = runner.invoke(cli.app, ["up", str(d), "--recursive", "--quiet", "--expiry", "1h"])
    assert r.exit_code == 0
    assert r.stdout.count("X-Amz-Signature") == 2

    # C3: every uploaded object must be recorded (none left untracked).
    rows = db.records_for("test")
    assert set(rows) == {"d/p.txt", "d/sub/q.txt"}


def test_up_json_is_a_typed_list(env, tmp_path):
    f = _file(tmp_path)
    r = runner.invoke(cli.app, ["up", str(f), "--json", "--expiry", "30m"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert isinstance(data, list) and len(data) == 1
    assert set(data[0]) == {"key", "size", "url", "link_type", "expires_at"}
    assert data[0]["link_type"] == "presigned"


def test_link_existing_and_missing(env, tmp_path):
    f = _file(tmp_path)
    runner.invoke(cli.app, ["up", str(f), "--quiet"])

    ok = runner.invoke(cli.app, ["link", "a.txt", "--quiet", "--expiry", "2h"])
    assert ok.exit_code == 0 and "X-Amz-Signature" in ok.stdout

    missing = runner.invoke(cli.app, ["link", "nope.txt", "--quiet"])
    assert missing.exit_code == 1
    assert "object not found" in (missing.stdout + str(missing.output))


def test_public_without_base_fails_fast(env, tmp_path, monkeypatch):
    monkeypatch.delenv("RINK_PUBLIC_BASE_URL")
    f = _file(tmp_path)
    r = runner.invoke(cli.app, ["up", str(f), "--public", "--quiet"])
    assert r.exit_code == 1
    assert "public" in (r.stdout + str(r.output)).lower()
    # Nothing should have been uploaded (failed before the network call).
    assert list(uploader.list_objects(env, "test")) == []


def test_name_validation_rejects_slashes(env, tmp_path):
    r = runner.invoke(cli.app, ["up", "-", "--name", "a/b", "--quiet"], input="data")
    assert r.exit_code == 1
    assert "plain filename" in (r.stdout + str(r.output))


def test_rm_deletes_and_clears_log(env, tmp_path):
    f = _file(tmp_path)
    runner.invoke(cli.app, ["up", str(f), "--quiet"])
    r = runner.invoke(cli.app, ["rm", "a.txt", "-y"])
    assert r.exit_code == 0 and "deleted" in r.stdout
    assert list(uploader.list_objects(env, "test")) == []
    assert "a.txt" not in db.records_for("test")


def test_prune_removes_expired(env, tmp_path):
    f = _file(tmp_path)
    runner.invoke(cli.app, ["up", str(f), "--quiet"])
    # Force the tracked link into the past.
    db.record("test", "a.txt", 5, "presigned", int(time.time()) - 10, "u")

    r = runner.invoke(cli.app, ["prune", "-y"])
    assert r.exit_code == 0 and "pruned" in r.stdout
    assert list(uploader.list_objects(env, "test")) == []


def test_stdin_upload(env):
    r = runner.invoke(cli.app, ["up", "-", "--name", "piped.txt", "--quiet"], input="from stdin")
    assert r.exit_code == 0 and "X-Amz-Signature" in r.stdout
    assert "piped.txt" in db.records_for("test")
