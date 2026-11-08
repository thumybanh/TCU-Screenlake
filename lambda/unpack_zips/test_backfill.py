"""Tests for backfill.py helper functions and core loop."""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import backfill as B


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zip_bytes(jpg_count: int = 3, extra_files: list[str] | None = None) -> bytes:
    """Return in-memory ZIP bytes with jpg_count fake JPGs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(jpg_count):
            zf.writestr(f"screenshot_{i:04d}.jpg", b"\xff\xd8" + b"\x00" * 10)
        for name in (extra_files or []):
            zf.writestr(name, b"csv,data\n1,2\n")
    return buf.getvalue()


def _manifest(uuids: list[str], total: int) -> dict:
    return {
        "_processed_zip_uuids": list(uuids),
        "total_screenshots": total,
    }


def _mock_s3(zip_keys: list[str], zip_bytes_map: dict[str, bytes], manifests: list[dict]):
    """Build a mock S3 client.

    zip_keys        — list of full S3 keys returned by list_objects_v2
    zip_bytes_map   — {basename: zip_bytes} for download_file
    manifests       — ordered list of manifest dicts returned by successive get_object calls
    """
    s3 = MagicMock()

    # list_objects_v2 paginator
    page = {"Contents": [{"Key": k} for k in zip_keys]}
    paginator = MagicMock()
    paginator.paginate.return_value = [page]
    s3.get_paginator.return_value = paginator

    # download_file: write the right bytes to the tmp path
    def _download(bucket, key, path):
        name = os.path.basename(key)
        data = zip_bytes_map.get(name, _make_zip_bytes(0))
        with open(path, "wb") as f:
            f.write(data)

    s3.download_file.side_effect = _download

    # get_object: return manifests in sequence
    manifest_iter = iter(manifests)

    def _get_object(Bucket, Key):
        m = next(manifest_iter)
        body = json.dumps(m).encode("utf-8")
        return {"Body": io.BytesIO(body)}

    s3.get_object.side_effect = _get_object
    return s3


def _ok_lambda_response(payload: dict | None = None) -> dict:
    body = json.dumps(payload or {"processed": 1}).encode("utf-8")
    return {"Payload": io.BytesIO(body), "FunctionError": None}


def _error_lambda_response(error_msg: str = "timeout") -> dict:
    return {
        "Payload": io.BytesIO(json.dumps({"errorMessage": error_msg}).encode()),
        "FunctionError": "Unhandled",
    }


# ---------------------------------------------------------------------------
# count_jpgs_in_zip
# ---------------------------------------------------------------------------

def test_count_jpgs_counts_only_jpg_files():
    data = _make_zip_bytes(jpg_count=5, extra_files=["data.csv", "log.txt"])
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        assert B.count_jpgs_in_zip(path) == 5
    finally:
        os.remove(path)


def test_count_jpgs_case_insensitive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("IMG.JPG", b"\xff\xd8")
        zf.writestr("img.jpg", b"\xff\xd8")
        zf.writestr("img.png", b"PNG")
    data = buf.getvalue()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        assert B.count_jpgs_in_zip(path) == 2
    finally:
        os.remove(path)


def test_count_jpgs_empty_zip():
    data = _make_zip_bytes(jpg_count=0)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        assert B.count_jpgs_in_zip(path) == 0
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------

def test_load_manifest_returns_empty_when_missing():
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
    )
    m = B.load_manifest(s3, "bucket", "some/key.json")
    assert m["_processed_zip_uuids"] == []
    assert m["total_screenshots"] == 0


def test_load_manifest_returns_parsed_json():
    s3 = MagicMock()
    data = {"_processed_zip_uuids": ["uuid-1"], "total_screenshots": 42}
    s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(data).encode())}
    m = B.load_manifest(s3, "bucket", "key.json")
    assert m["total_screenshots"] == 42
    assert "uuid-1" in m["_processed_zip_uuids"]


# ---------------------------------------------------------------------------
# build_s3_event
# ---------------------------------------------------------------------------

def test_build_s3_event_structure():
    ev = B.build_s3_event("my-bucket", "some/path/file.zip")
    assert len(ev["Records"]) == 1
    rec = ev["Records"][0]
    assert rec["s3"]["bucket"]["name"] == "my-bucket"
    assert rec["s3"]["object"]["key"] == "some/path/file.zip"


# ---------------------------------------------------------------------------
# run_backfill — already-processed ZIP skipped with --resume
# ---------------------------------------------------------------------------

def test_resume_skips_already_processed_zip():
    """A ZIP whose UUID is already in the manifest must be skipped, not re-invoked."""
    zip_name = "image_zip_abc_5.zip"
    zip_uuid = "image_zip_abc_5"
    zip_data = _make_zip_bytes(5)

    initial_manifest = _manifest([zip_uuid], total=5)

    s3 = _mock_s3(
        zip_keys=[f"prefix/{zip_name}"],
        zip_bytes_map={zip_name: zip_data},
        # load_manifest called twice: initial load + final verification
        manifests=[initial_manifest, initial_manifest],
    )
    lam = MagicMock()

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="prefix/",
        manifest_key="manifest.json",
        function_name="fn",
        resume=True,
    )

    assert ok is True
    lam.invoke.assert_not_called()


def test_resume_does_not_skip_without_flag():
    """Without --resume, already-processed ZIPs are re-invoked."""
    zip_name = "image_zip_abc_5.zip"
    zip_uuid = "image_zip_abc_5"
    zip_data = _make_zip_bytes(5)

    initial_manifest = _manifest([zip_uuid], total=5)
    # After re-invoke, manifest still shows same UUID (Lambda's idempotency skip)
    post_invoke_manifest = _manifest([zip_uuid], total=5)

    s3 = _mock_s3(
        zip_keys=[f"prefix/{zip_name}"],
        zip_bytes_map={zip_name: zip_data},
        manifests=[initial_manifest, post_invoke_manifest, post_invoke_manifest],
    )
    lam = MagicMock()
    lam.invoke.return_value = _ok_lambda_response()

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="prefix/",
        manifest_key="manifest.json",
        function_name="fn",
        resume=False,
    )

    assert ok is True
    lam.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# run_backfill — new ZIP increments screenshot count correctly
# ---------------------------------------------------------------------------

def test_new_zip_increments_screenshot_count():
    zip_name = "image_zip_new_7.zip"
    zip_uuid = "image_zip_new_7"
    zip_data = _make_zip_bytes(7)

    empty_manifest = _manifest([], total=0)
    post_manifest = _manifest([zip_uuid], total=7)

    s3 = _mock_s3(
        zip_keys=[f"prefix/{zip_name}"],
        zip_bytes_map={zip_name: zip_data},
        manifests=[empty_manifest, post_manifest, post_manifest],
    )
    lam = MagicMock()
    lam.invoke.return_value = _ok_lambda_response()

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="prefix/",
        manifest_key="manifest.json",
        function_name="fn",
    )

    assert ok is True
    lam.invoke.assert_called_once()


def test_screenshot_count_mismatch_stops_backfill():
    """If manifest total increases by wrong amount, backfill stops."""
    zip_name = "image_zip_bad_3.zip"
    zip_uuid = "image_zip_bad_3"
    zip_data = _make_zip_bytes(3)   # 3 JPGs expected

    empty_manifest = _manifest([], total=0)
    # Manifest says UUID is recorded but total only went up by 2, not 3
    wrong_manifest = _manifest([zip_uuid], total=2)

    s3 = _mock_s3(
        zip_keys=[f"prefix/{zip_name}"],
        zip_bytes_map={zip_name: zip_data},
        manifests=[empty_manifest, wrong_manifest],
    )
    lam = MagicMock()
    lam.invoke.return_value = _ok_lambda_response()

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="prefix/",
        manifest_key="manifest.json",
        function_name="fn",
    )

    assert ok is False


# ---------------------------------------------------------------------------
# run_backfill — failed invocation stops immediately
# ---------------------------------------------------------------------------

def test_failed_lambda_invocation_stops_backfill():
    zip_name = "image_zip_fail_2.zip"
    zip_data = _make_zip_bytes(2)

    s3 = _mock_s3(
        zip_keys=[f"prefix/{zip_name}"],
        zip_bytes_map={zip_name: zip_data},
        manifests=[_manifest([], total=0)],
    )
    lam = MagicMock()
    lam.invoke.return_value = _error_lambda_response("runtime error")

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="prefix/",
        manifest_key="manifest.json",
        function_name="fn",
    )

    assert ok is False


def test_uuid_missing_from_manifest_after_invoke_stops_backfill():
    """Lambda returned success but UUID not in manifest — stop and report."""
    zip_name = "image_zip_ghost_4.zip"
    zip_uuid = "image_zip_ghost_4"
    zip_data = _make_zip_bytes(4)

    empty_manifest = _manifest([], total=0)
    # Manifest after invoke still empty — Lambda skipped for some reason
    still_empty = _manifest([], total=0)

    s3 = _mock_s3(
        zip_keys=[f"prefix/{zip_name}"],
        zip_bytes_map={zip_name: zip_data},
        manifests=[empty_manifest, still_empty],
    )
    lam = MagicMock()
    lam.invoke.return_value = _ok_lambda_response()

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="prefix/",
        manifest_key="manifest.json",
        function_name="fn",
    )

    assert ok is False


# ---------------------------------------------------------------------------
# run_backfill — resume after partial failure
# ---------------------------------------------------------------------------

def test_resume_after_partial_failure_processes_remaining():
    """First ZIP already processed (from prior run), second ZIP is new.
    --resume skips first, processes second."""
    zip1 = "image_zip_done_5.zip"
    uuid1 = "image_zip_done_5"
    zip2 = "image_zip_new_3.zip"
    uuid2 = "image_zip_new_3"
    data1 = _make_zip_bytes(5)
    data2 = _make_zip_bytes(3)

    # Initial manifest already has zip1
    initial = _manifest([uuid1], total=5)
    # After processing zip2
    after_zip2 = _manifest([uuid1, uuid2], total=8)

    s3 = _mock_s3(
        zip_keys=[f"p/{zip1}", f"p/{zip2}"],
        zip_bytes_map={zip1: data1, zip2: data2},
        manifests=[initial, after_zip2, after_zip2],
    )
    lam = MagicMock()
    lam.invoke.return_value = _ok_lambda_response()

    ok = B.run_backfill(
        s3=s3, lam=lam,
        bucket="bucket", prefix="p/",
        manifest_key="manifest.json",
        function_name="fn",
        resume=True,
    )

    assert ok is True
    # Only zip2 should have been invoked
    assert lam.invoke.call_count == 1
    invoked_key = lam.invoke.call_args[1]["Payload"]
    if isinstance(invoked_key, bytes):
        payload = json.loads(invoked_key)
    else:
        payload = json.loads(invoked_key.decode("utf-8") if hasattr(invoked_key, "decode") else invoked_key)
    assert zip2 in payload["Records"][0]["s3"]["object"]["key"]
