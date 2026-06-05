"""End-to-end CLI tests driving real commands against a mocked S3 (moto)."""

import json
import time

import boto3
import pytest
from botocore.config import Config as BotoConfig
from moto import mock_aws
from typer.testing import CliRunner

from rink import cli, config as cfgmod, db, serve as serve_mod, uploader

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Isolate config: no real file, credentials via env.
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
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


def test_serve_init_writes_worker_template(env, tmp_path):
    worker_dir = tmp_path / "worker"

    r = runner.invoke(cli.app, ["serve", "--init", "--worker-dir", str(worker_dir)])

    assert r.exit_code == 0
    assert (worker_dir / "wrangler.jsonc").exists()
    assert (worker_dir / "src" / "index.js").exists()
    assert (worker_dir / ".dev.vars").exists()
    assert not (worker_dir / "__init__.py").exists()
    assert ".dev.vars" in (worker_dir / ".gitignore").read_text()
    assert "replace-with" in (worker_dir / ".dev.vars.example").read_text()
    assert '"bucket_name": "test"' in (worker_dir / "wrangler.jsonc").read_text()
    assert "RINK_SERVE_TOKEN" in r.stdout


def test_serve_init_deploy_saves_worker_config(env, tmp_path, monkeypatch):
    worker_dir = tmp_path / "worker"
    worker_url = "https://rink-serve.example.workers.dev"
    synced = []

    monkeypatch.setattr(serve_mod, "new_admin_token", lambda: "generated-token")
    monkeypatch.setattr(
        serve_mod,
        "deploy_worker",
        lambda path: serve_mod.DeployResult(
            output=f"Deployed {worker_url}\n",
            worker_url=worker_url,
        ),
    )
    monkeypatch.setattr(
        serve_mod,
        "put_worker_secret",
        lambda path, token: synced.append((path, token)) or "Uploaded secret\n",
    )

    r = runner.invoke(
        cli.app,
        ["serve", "--init", "--deploy", "--worker-dir", str(worker_dir)],
    )

    assert r.exit_code == 0, r.output
    raw = cfgmod.read_raw()
    assert raw["serve_url"] == worker_url
    assert raw["serve_token"] == "generated-token"
    assert synced == [(worker_dir, "generated-token")]
    assert "saved Serve config" in r.stdout


def test_serve_deploy_saves_url_and_token_from_dev_vars(env, tmp_path, monkeypatch):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / ".dev.vars").write_text("RINK_SERVE_ADMIN_TOKEN=local-token\n")
    worker_url = "https://rink-serve.example.workers.dev"
    synced = []

    monkeypatch.setattr(
        serve_mod,
        "deploy_worker",
        lambda path: serve_mod.DeployResult(
            output=f"Deployed {worker_url}\n",
            worker_url=worker_url,
        ),
    )
    monkeypatch.setattr(
        serve_mod,
        "put_worker_secret",
        lambda path, token: synced.append((path, token)) or "Uploaded secret\n",
    )

    r = runner.invoke(cli.app, ["serve", "--deploy", "--worker-dir", str(worker_dir)])

    assert r.exit_code == 0, r.output
    raw = cfgmod.read_raw()
    assert raw["serve_url"] == worker_url
    assert raw["serve_token"] == "local-token"
    assert synced == [(worker_dir, "local-token")]


def test_serve_deploy_prompts_when_wrangler_url_is_missing(env, tmp_path, monkeypatch):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / ".dev.vars").write_text("RINK_SERVE_ADMIN_TOKEN=local-token\n")
    worker_url = "https://manual.example.workers.dev"
    synced = []

    monkeypatch.setattr(
        serve_mod,
        "deploy_worker",
        lambda path: serve_mod.DeployResult(output="Deployed rink-serve\n", worker_url=None),
    )
    monkeypatch.setattr(
        serve_mod,
        "put_worker_secret",
        lambda path, token: synced.append((path, token)) or "Uploaded secret\n",
    )

    r = runner.invoke(
        cli.app,
        ["serve", "--deploy", "--worker-dir", str(worker_dir)],
        input=f"{worker_url}\n",
    )

    assert r.exit_code == 0, r.output
    raw = cfgmod.read_raw()
    assert raw["serve_url"] == worker_url
    assert raw["serve_token"] == "local-token"
    assert synced == [(worker_dir, "local-token")]
    assert "Serve Worker URL" in r.stdout


def test_serve_deploy_accepts_worker_url_option(env, tmp_path, monkeypatch):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    worker_url = "https://manual.example.workers.dev"
    synced = []

    monkeypatch.setattr(
        serve_mod,
        "deploy_worker",
        lambda path: serve_mod.DeployResult(output="Deployed rink-serve\n", worker_url=None),
    )
    monkeypatch.setattr(
        serve_mod,
        "put_worker_secret",
        lambda path, token: synced.append((path, token)) or "Uploaded secret\n",
    )

    r = runner.invoke(
        cli.app,
        [
            "serve",
            "--deploy",
            "--worker-dir",
            str(worker_dir),
            "--worker-url",
            worker_url,
            "--token",
            "manual-token",
        ],
    )

    assert r.exit_code == 0, r.output
    raw = cfgmod.read_raw()
    assert raw["serve_url"] == worker_url
    assert raw["serve_token"] == "manual-token"
    assert synced == [(worker_dir, "manual-token")]


def test_serve_creates_receive_link(env, monkeypatch):
    calls = []

    def fake_create(worker_url, token, **kwargs):
        calls.append((worker_url, token, kwargs))
        return serve_mod.ReceiveLink(
            id="drop1",
            upload_url="https://serve.example/r/drop1",
            expires_at=(int(time.time()) + 3600) * 1000,
            max_uploads=1,
            max_bytes=10 * 1024 * 1024,
            max_download_views=1,
            prefix=kwargs["prefix"],
        )

    monkeypatch.setenv("RINK_SERVE_URL", "https://serve.example")
    monkeypatch.setenv("RINK_SERVE_TOKEN", "tok")
    monkeypatch.setattr(serve_mod, "create_receive_link", fake_create)

    r = runner.invoke(
        cli.app,
        ["serve", "--quiet", "--prefix", "dropbox", "--max-size", "10MB", "--expiry", "1h"],
    )

    assert r.exit_code == 0
    assert r.stdout.strip() == "https://serve.example/r/drop1"
    assert calls == [
        (
            "https://serve.example",
            "tok",
            {
                "prefix": "dropbox",
                "label": None,
                "ttl_seconds": 3600,
                "max_uploads": 1,
                "max_bytes": 10 * 1024 * 1024,
                "max_download_views": 1,
            },
        )
    ]


def test_serve_unauthorized_suggests_secret_sync(env, monkeypatch):
    def fake_create(worker_url, token, **kwargs):
        raise RuntimeError('Worker rejected request (401): {"error": "unauthorized"}')

    monkeypatch.setenv("RINK_SERVE_URL", "https://serve.example")
    monkeypatch.setenv("RINK_SERVE_TOKEN", "tok")
    monkeypatch.setattr(serve_mod, "create_receive_link", fake_create)

    r = runner.invoke(cli.app, ["serve", "--quiet"])

    assert r.exit_code == 1
    output = r.stdout + r.stderr
    assert "does not match the deployed Worker secret" in output
    assert "rink" in output
    assert "serve --deploy --worker-dir rink-serve-worker" in output


def test_serve_requires_worker_config(env, monkeypatch):
    monkeypatch.delenv("RINK_SERVE_URL", raising=False)
    monkeypatch.delenv("RINK_SERVE_TOKEN", raising=False)

    r = runner.invoke(cli.app, ["serve", "--quiet"])

    assert r.exit_code == 1
    assert "Serve Worker URL" in (r.stdout + r.output)
