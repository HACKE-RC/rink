"""S3/R2 interaction tests against a mocked backend (moto)."""

import boto3
import pytest
from botocore.config import Config as BotoConfig
from moto import mock_aws

from rink import links, uploader


@pytest.fixture
def s3():
    with mock_aws():
        # Match production: SigV4 so presigned URLs use X-Amz-* params.
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            config=BotoConfig(signature_version="s3v4"),
        )
        client.create_bucket(Bucket="test")
        yield client


def test_upload_list_head_delete(s3, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello")

    uploader.upload_file(s3, "test", f, "f.txt")
    objs = list(uploader.list_objects(s3, "test"))
    assert [o["Key"] for o in objs] == ["f.txt"]

    head = uploader.head_object(s3, "test", "f.txt")
    assert head["ContentLength"] == 5

    uploader.delete_object(s3, "test", "f.txt")
    assert list(uploader.list_objects(s3, "test")) == []


def test_content_type_and_disposition(s3, tmp_path):
    f = tmp_path / "f.pdf"
    f.write_bytes(b"%PDF-1.4 test")

    uploader.upload_file(
        s3, "test", f, "f.pdf", extra={"ContentDisposition": "attachment"}
    )
    head = uploader.head_object(s3, "test", "f.pdf")
    assert head["ContentType"] == "application/pdf"
    assert head["ContentDisposition"] == "attachment"


def test_list_prefix(s3, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    uploader.upload_file(s3, "test", f, "dir/a.txt")
    uploader.upload_file(s3, "test", f, "other.txt")
    keys = [o["Key"] for o in uploader.list_objects(s3, "test", "dir/")]
    assert keys == ["dir/a.txt"]


def test_presigned_url(s3, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    uploader.upload_file(s3, "test", f, "f.txt")
    url = links.presigned_url(s3, "test", "f.txt", 3600)
    assert "X-Amz-Signature" in url
    assert "f.txt" in url


def test_zip_folder_and_iter(tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    (d / "sub" / "b.txt").write_text("b")

    arc = uploader.zip_folder(d)
    assert arc.exists()
    assert arc.name == "d.zip"

    rels = [rel for _, rel in uploader.iter_files(d)]
    assert rels == ["a.txt", "sub/b.txt"]
