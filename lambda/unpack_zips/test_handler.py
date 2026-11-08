"""Local unit tests for handler.py — do not run in Lambda.
Run: python -m pytest lambda/unpack_zips/test_handler.py -v
"""
from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

import pytest

import handler as H


def _build_zip(rows, jpg_names, extra_csvs=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        header = [
            "id_user", "file", "zipFileId", "apk", "id_session",
            "id_segment", "app", "text", "weekday", "t_epoch_ts_ms",
            "t_natural_utc_ts", "t_natural_second_ts", "t_natural_day_ts",
        ]
        lines = [",".join(f'"{h}"' for h in header)]
        for r in rows:
            lines.append(",".join(f'"{r.get(h, "")}"' for h in header))
        csv_body = ("\n".join(lines) + "\n").encode("utf-8")
        zf.writestr("screenshot_data_csv_batch1.csv", csv_body)
        for name in jpg_names:
            zf.writestr(name, b"\xff\xd8\xff\xe0FAKE-JPEG-BYTES")
        for name, body in (extra_csvs or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


class FakeS3:
    """In-memory S3 that lets tests exercise get/put/download semantics."""

    def __init__(self, zip_bytes, prefills: dict | None = None):
        self.zip_bytes = zip_bytes
        self.puts: list[dict] = []
        self.store: dict[str, bytes] = dict(prefills or {})

    def download_file(self, bucket, key, local_path):
        with open(local_path, "wb") as fh:
            fh.write(self.zip_bytes)

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.store[kwargs["Key"]] = kwargs["Body"]
        return {"ETag": "fake"}

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        body = self.store[Key]
        return {"Body": io.BytesIO(body)}


DEFAULT_KEY = "academia/tenant/T_A/panel/general_1/V/panelist/2323/image_zip_uuid_2.zip"


def _run(zip_bytes, key=DEFAULT_KEY, prefills=None):
    fake = FakeS3(zip_bytes, prefills=prefills)
    with patch.object(H, "s3", fake):
        H.process_zip("bucket", key)
    return fake


def _index_from(fake):
    key = "_manifests/panel=general_1/panelist=2323/_index.json"
    return json.loads(fake.store[key].decode("utf-8"))


def test_first_zip_creates_index_with_summary_fields():
    rows = [
        {"file": "/dev/img_a.jpg", "apk": "com.android.chrome",
         "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"},
        {"file": "/dev/img_b.jpg", "apk": "com.android.chrome",
         "id_session": "sess-1", "t_epoch_ts_ms": "1788526810000"},
    ]
    fake = _run(_build_zip(rows, ["img_a.jpg", "img_b.jpg"]))
    index = _index_from(fake)
    assert index["panel"] == "general_1"
    assert index["panelist"] == "2323"
    assert index["study_start_utc"] == "2026-09-04T13:00:05Z"
    assert index["latest_capture_utc"] == "2026-09-04T13:00:10Z"
    assert index["total_screenshots"] == 2
    assert index["days_participating"] == 1
    assert index["_processed_zip_uuids"] == ["image_zip_uuid_2"]
    assert index["updated_at"].endswith("Z")


def test_second_zip_updates_running_totals():
    rows_first = [{"file": "/dev/img_a.jpg", "apk": "com.a",
                   "id_session": "s1", "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows_first, ["img_a.jpg"]),
                key="academia/tenant/T_A/panel/general_1/V/panelist/2323/image_zip_first_1.zip")

    rows_second = [
        {"file": "/dev/img_c.jpg", "apk": "com.a", "id_session": "s2",
         "t_epoch_ts_ms": "1789131605000"},  # 7 days later
        {"file": "/dev/img_d.jpg", "apk": "com.a", "id_session": "s2",
         "t_epoch_ts_ms": "1789131610000"},
    ]
    prefill_key = "_manifests/panel=general_1/panelist=2323/_index.json"
    fake2 = _run(_build_zip(rows_second, ["img_c.jpg", "img_d.jpg"]),
                 key="academia/tenant/T_A/panel/general_1/V/panelist/2323/image_zip_second_2.zip",
                 prefills={prefill_key: fake.store[prefill_key]})
    index = _index_from(fake2)
    assert index["total_screenshots"] == 3
    assert index["study_start_utc"] == "2026-09-04T13:00:05Z"  # unchanged
    assert index["latest_capture_utc"] == "2026-09-11T13:00:10Z"
    assert index["days_participating"] == 8  # inclusive calendar span
    assert index["_processed_zip_uuids"] == [
        "image_zip_first_1", "image_zip_second_2",
    ]


def test_replay_same_zip_is_noop():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]))
    index_after_first = _index_from(fake)

    fake2 = _run(_build_zip(rows, ["img_a.jpg"]),
                 prefills={k: v for k, v in fake.store.items()})
    index_after_second = _index_from(fake2)
    assert index_after_first == index_after_second


def test_csvs_replicated_across_midnight_split_dates():
    rows = [
        {"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
         "t_epoch_ts_ms": "1788479999000"},  # 2026-09-03 UTC
        {"file": "/dev/img_b.jpg", "apk": "com.a", "id_session": "s1",
         "t_epoch_ts_ms": "1788480001000"},  # 2026-09-04 UTC
    ]
    fake = _run(_build_zip(rows, ["img_a.jpg", "img_b.jpg"]))
    csv_keys = [p["Key"] for p in fake.puts if p["Key"].startswith("data/")]
    dates = {k.split("date=")[1].split("/")[0] for k in csv_keys}
    assert dates == {"2026-09-03", "2026-09-04"}


def test_csvs_have_text_csv_content_type():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]))
    for put in fake.puts:
        if put["Key"].startswith("data/"):
            assert put["ContentType"] == "text/csv"
        elif put["Key"].startswith("_manifests/"):
            assert put["ContentType"] == "application/json"


def test_no_images_are_extracted():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]))
    assert not any(p["Key"].endswith(".jpg") for p in fake.puts)
    assert not any(p["Key"].startswith("unpacked/") for p in fake.puts)


def test_missing_csv_row_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "1788480001000"}]
    with pytest.raises(H.HandlerError, match="no screenshot_data row"):
        _run(_build_zip(rows, ["img_a.jpg", "img_MYSTERY.jpg"]))


def test_missing_columns_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("screenshot_data_csv_x.csv", b'"file"\n"a.jpg"\n')
        zf.writestr("img_a.jpg", b"data")
    with pytest.raises(H.HandlerError, match="missing columns"):
        _run(buf.getvalue())


def test_empty_session_id_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "",
             "t_epoch_ts_ms": "1788480001000"}]
    with pytest.raises(H.HandlerError, match="empty id_session"):
        _run(_build_zip(rows, ["img_a.jpg"]))


def test_bad_epoch_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "not-a-number"}]
    with pytest.raises(H.HandlerError, match="invalid t_epoch_ts_ms"):
        _run(_build_zip(rows, ["img_a.jpg"]))


def test_key_not_matching_layout_is_skipped():
    fake = FakeS3(_build_zip([], []))
    with patch.object(H, "s3", fake):
        H.process_zip("bucket", "somewhere_else/foo.zip")
    assert fake.puts == []


# ---------------------------------------------------------------------------
# RS researcher ZIP routing — academia/testing/tenant/ paths
# ---------------------------------------------------------------------------

RS_KEY = "academia/testing/tenant/T_A/panel/general_1/V/panelist/RS0001/image_zip_uuid_2.zip"


def _rs_index_from(fake):
    key = "academia/testing/_manifests/panel=general_1/panelist=RS0001/_index.json"
    return json.loads(fake.store[key].decode("utf-8"))


def test_rs_zip_routes_to_testing_prefix():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a",
             "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]), key=RS_KEY)
    csv_keys = [p["Key"] for p in fake.puts if p["Key"].endswith(".csv")]
    assert all(k.startswith("academia/testing/data/") for k in csv_keys), \
        f"RS CSV not under academia/testing/data/: {csv_keys}"


def test_rs_manifest_routes_to_testing_prefix():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a",
             "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]), key=RS_KEY)
    manifest_keys = [p["Key"] for p in fake.puts if p["Key"].endswith("_index.json")]
    assert all(k.startswith("academia/testing/_manifests/") for k in manifest_keys), \
        f"RS manifest not under academia/testing/_manifests/: {manifest_keys}"


def test_rs_manifest_has_correct_panelist():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a",
             "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]), key=RS_KEY)
    index = _rs_index_from(fake)
    assert index["panelist"] == "RS0001"
    assert index["panel"] == "general_1"


def test_rs_zip_does_not_write_to_lf_data_prefix():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a",
             "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]), key=RS_KEY)
    lf_writes = [p["Key"] for p in fake.puts
                 if p["Key"].startswith("data/") or p["Key"].startswith("_manifests/")]
    assert lf_writes == [], f"RS ZIP wrote to LF tree: {lf_writes}"


def test_lf_zip_does_not_write_to_rs_prefix():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a",
             "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"}]
    fake = _run(_build_zip(rows, ["img_a.jpg"]))  # DEFAULT_KEY is LF path
    rs_writes = [p["Key"] for p in fake.puts if p["Key"].startswith("academia/testing/")]
    assert rs_writes == [], f"LF ZIP wrote to RS tree: {rs_writes}"


def test_rs_idempotent_replay():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a",
             "id_session": "s1", "t_epoch_ts_ms": "1788526805000"}]
    fake1 = _run(_build_zip(rows, ["img_a.jpg"]), key=RS_KEY)
    index_key = "academia/testing/_manifests/panel=general_1/panelist=RS0001/_index.json"
    fake2 = _run(_build_zip(rows, ["img_a.jpg"]), key=RS_KEY,
                 prefills={k: v for k, v in fake1.store.items()})
    # Second run is a no-op — index unchanged
    assert json.loads(fake1.store[index_key]) == json.loads(fake2.store[index_key])
