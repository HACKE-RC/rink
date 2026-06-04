"""SQLite log tests, using a temp DB path."""

import pytest

from rink import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rink.db")
    return db


def test_record_and_list(tmp_db):
    tmp_db.record("b", "k.txt", 10, "presigned", 12345, "http://x")
    rows = tmp_db.records_for("b")
    assert "k.txt" in rows
    assert rows["k.txt"]["expires_at"] == 12345
    assert rows["k.txt"]["size"] == 10


def test_upsert_replaces_row(tmp_db):
    tmp_db.record("b", "k", 1, "presigned", 100, "u1")
    tmp_db.record("b", "k", 2, "public", None, "u2")
    rows = tmp_db.records_for("b")
    assert rows["k"]["size"] == 2
    assert rows["k"]["link_type"] == "public"
    assert rows["k"]["expires_at"] is None


def test_prefix_filter_and_delete(tmp_db):
    tmp_db.record("b", "dir/a", 1, "presigned", 1, "u")
    tmp_db.record("b", "other", 1, "presigned", 1, "u")
    assert set(tmp_db.records_for("b", "dir/")) == {"dir/a"}
    tmp_db.delete("b", "dir/a")
    assert "dir/a" not in tmp_db.records_for("b")


def test_buckets_are_isolated(tmp_db):
    tmp_db.record("b1", "k", 1, "presigned", 1, "u")
    tmp_db.record("b2", "k", 1, "presigned", 1, "u")
    assert set(tmp_db.records_for("b1")) == {"k"}
    assert set(tmp_db.records_for("b2")) == {"k"}
    tmp_db.delete("b1", "k")
    assert set(tmp_db.records_for("b2")) == {"k"}
