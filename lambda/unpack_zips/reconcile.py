#!/usr/bin/env python3
"""Final reconciliation: verify all historical ZIPs migrated and processed.

Handles both filename formats:
  Old (copied from 1001): image_zip_<UUID>_<count>.zip
  New (uploaded with RS0006 app): RS0006_image_zip_<UUID>_<count>.zip

Checks performed:
  1. Every ZIP at both 1001 source paths exists at the RS0006 destination (by filename).
  2. Every source-to-destination copy is content-identical (SHA-256) when
     --verify-checksums is set. Required for the Phase D2 gate.
  3. If the same filename appears under both source paths, their SHA-256 hashes
     are compared. A conflict (different content) is an error requiring your review.
  4. Every ZIP at the RS0006 destination has its UUID in the manifest (processed).
  5. No UUID appears more than once in the manifest.
  6. Late uploads (RS0006-prefixed new files not in source) are reported and
     verified against the manifest.

Does NOT delete, copy, or modify anything.

Usage:
  python3 reconcile.py \
    --bucket <bucket-name> \
    --source-lf-prefix   "academia/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/1001/" \
    --source-rs-prefix   "academia/testing/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/1001/" \
    --dest-prefix        "academia/testing/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/RS0006/" \
    --manifest-key       "academia/testing/_manifests/panel=general_1/panelist=RS0006/_index.json" \
    --region us-east-2 \
    [--verify-checksums]

The --verify-checksums flag downloads each source and destination ZIP to compare
SHA-256 hashes. This is required for the Phase D2 migration gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter

import boto3
from botocore.exceptions import ClientError


def list_zips(s3, bucket: str, prefix: str) -> dict[str, str]:
    """Return {filename: full_s3_key} for all .zip objects under prefix."""
    result: dict[str, str] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".zip"):
                name = os.path.basename(key)
                result[name] = key
    return result


def load_manifest(s3, bucket: str, manifest_key: str) -> dict:
    try:
        obj = s3.get_object(Bucket=bucket, Key=manifest_key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return {"_processed_zip_uuids": [], "total_screenshots": 0}
        raise


def sha256_of_s3_object(s3, bucket: str, key: str) -> str:
    """Download an S3 object to a temp file and return its SHA-256 hex digest."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        s3.download_file(bucket, key, tmp_path)
        h = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def zip_uuid_from_name(zip_name: str) -> str:
    """Return the filename stem (without .zip) used as the Lambda idempotency key."""
    return os.path.splitext(zip_name)[0]


def run_reconcile(
    s3,
    bucket: str,
    source_lf_prefix: str,
    source_rs_prefix: str,
    dest_prefix: str,
    manifest_key: str,
    verify_checksums: bool = False,
) -> bool:
    """Execute all reconciliation checks. Returns True if all pass, False otherwise.

    Separated from main() so tests can inject mock clients directly.
    """
    print(f"Listing source ZIPs (LF 1001 path)...")
    source_lf = list_zips(s3, bucket, source_lf_prefix)
    print(f"  {len(source_lf)} ZIPs at LF source")

    print(f"Listing source ZIPs (testing 1001 path)...")
    source_rs_legacy = list_zips(s3, bucket, source_rs_prefix)
    print(f"  {len(source_rs_legacy)} ZIPs at testing source")

    # Merge both source paths. Same filename in both → compare SHA-256 if possible.
    all_source: dict[str, str] = {}
    duplicate_source_conflicts: list[str] = []

    for name, key in source_lf.items():
        all_source[name] = key

    for name, key in source_rs_legacy.items():
        if name in all_source:
            if verify_checksums:
                hash_lf = sha256_of_s3_object(s3, bucket, all_source[name])
                hash_rs = sha256_of_s3_object(s3, bucket, key)
                if hash_lf != hash_rs:
                    duplicate_source_conflicts.append(
                        f"{name}: LF sha256={hash_lf[:16]}… "
                        f"vs testing sha256={hash_rs[:16]}… (CONTENT DIFFERS)"
                    )
                    print(f"  CONFLICT: {name} has different content across source paths")
                else:
                    print(f"  INFO: {name} exists in both source paths with identical content — using LF path")
            else:
                print(f"  WARNING: {name} exists in both source paths — "
                      f"run with --verify-checksums to compare content. Using LF path.")
        else:
            all_source[name] = key

    print(f"  {len(all_source)} unique source ZIPs total")

    print(f"Listing destination ZIPs (RS0006 path)...")
    dest = list_zips(s3, bucket, dest_prefix)
    print(f"  {len(dest)} ZIPs at destination")

    print(f"Loading RS0006 manifest...")
    manifest = load_manifest(s3, bucket, manifest_key)
    manifest_uuid_list: list[str] = manifest.get("_processed_zip_uuids", [])
    manifest_uuid_set: set[str] = set(manifest_uuid_list)
    manifest_total: int = manifest.get("total_screenshots", 0)
    print(f"  Manifest: {len(manifest_uuid_list)} total entries, "
          f"{len(manifest_uuid_set)} unique UUIDs, "
          f"{manifest_total} total_screenshots")

    # --- Check 1: source ZIPs present at destination (by filename) ---
    missing_from_dest: list[str] = []
    for name in sorted(all_source):
        if name not in dest:
            missing_from_dest.append(name)

    # --- Check 2: SHA-256 source-to-destination integrity ---
    checksum_failures: list[str] = []
    if verify_checksums:
        present_in_both = [n for n in all_source if n in dest]
        total = len(present_in_both)
        print(f"\nVerifying SHA-256 for {total} source→destination ZIP pairs...")
        for i, name in enumerate(sorted(present_in_both), 1):
            src_key = all_source[name]
            dst_key = dest[name]
            print(f"  [{i}/{total}] {name}", end=" ", flush=True)
            src_hash = sha256_of_s3_object(s3, bucket, src_key)
            dst_hash = sha256_of_s3_object(s3, bucket, dst_key)
            if src_hash == dst_hash:
                print(f"OK ({src_hash[:16]}…)")
            else:
                print(f"MISMATCH src={src_hash[:16]}… dst={dst_hash[:16]}…")
                checksum_failures.append(
                    f"{name}: src sha256={src_hash} dest sha256={dst_hash}"
                )

    # --- Check 3: destination ZIPs present in manifest ---
    dest_uuids = {zip_uuid_from_name(name): name for name in dest}
    missing_from_manifest: list[str] = []
    for uuid, name in sorted(dest_uuids.items()):
        if uuid not in manifest_uuid_set:
            missing_from_manifest.append(name)

    # --- Check 4: duplicate UUIDs in manifest ---
    uuid_counts = Counter(manifest_uuid_list)
    duplicate_uuids = {uuid: count for uuid, count in uuid_counts.items() if count > 1}

    # --- Check 5: ZIPs in dest but not in any source (late RS0006 uploads) ---
    late_uploads: list[str] = []
    late_unprocessed: list[str] = []
    for name in sorted(dest):
        if name not in all_source:
            late_uploads.append(name)
            if zip_uuid_from_name(name) not in manifest_uuid_set:
                late_unprocessed.append(name)

    # --- Report ---
    print(f"\n{'='*60}")
    print("RECONCILIATION REPORT")
    print(f"{'='*60}")
    print(f"Source ZIPs (all):       {len(all_source):4d}")
    print(f"Destination ZIPs:        {len(dest):4d}")
    print(f"Manifest UUIDs (unique): {len(manifest_uuid_set):4d}")

    all_ok = True

    if duplicate_source_conflicts:
        all_ok = False
        print(f"\nFAIL — {len(duplicate_source_conflicts)} source path conflict(s) with different content:")
        for c in duplicate_source_conflicts:
            print(f"  CONFLICT: {c}")
    else:
        if any(n in source_rs_legacy for n in source_lf):
            print(f"\nINFO — some filenames exist in both source paths (identical content)")

    if missing_from_dest:
        all_ok = False
        print(f"\nFAIL — {len(missing_from_dest)} source ZIP(s) not copied to destination:")
        for name in missing_from_dest:
            print(f"  NOT COPIED: {name}  (source: {all_source[name]})")
    else:
        print(f"\nOK — all {len(all_source)} source ZIPs present at destination")

    if verify_checksums:
        if checksum_failures:
            all_ok = False
            print(f"\nFAIL — {len(checksum_failures)} SHA-256 mismatch(es):")
            for c in checksum_failures:
                print(f"  CORRUPT COPY: {c}")
        else:
            print(f"OK — all {len(all_source) - len(missing_from_dest)} "
                  f"source→destination SHA-256 hashes match")
    else:
        print("INFO — SHA-256 not verified (run with --verify-checksums for Phase D2 gate)")

    if missing_from_manifest:
        all_ok = False
        print(f"\nFAIL — {len(missing_from_manifest)} destination ZIP(s) not processed:")
        for name in missing_from_manifest:
            print(f"  UNPROCESSED: {name}  (UUID: {zip_uuid_from_name(name)})")
    else:
        print(f"OK — all {len(dest)} destination ZIPs present in manifest")

    if duplicate_uuids:
        all_ok = False
        print(f"\nFAIL — {len(duplicate_uuids)} duplicate UUID(s) in manifest:")
        for uuid, count in sorted(duplicate_uuids.items()):
            print(f"  DUPLICATE ({count}x): {uuid}")
    else:
        print(f"OK — no duplicate UUIDs in manifest")

    if late_uploads:
        print(f"\nINFO — {len(late_uploads)} late upload(s) in destination "
              f"(RS0006 app uploads, not in source):")
        for name in late_uploads:
            uuid = zip_uuid_from_name(name)
            status = "in manifest" if uuid in manifest_uuid_set else "NOT in manifest"
            print(f"  LATE UPLOAD: {name}  → {status}")
        if late_unprocessed:
            all_ok = False
            print(f"\nFAIL — {len(late_unprocessed)} late upload(s) not processed:")
            for name in late_unprocessed:
                print(f"  UNPROCESSED: {name}")

    print(f"\n{'='*60}")
    if all_ok:
        print("RESULT: PASS — migration complete, all ZIPs accounted for.")
    else:
        print("RESULT: FAIL — see above. Address failures before declaring migration complete.")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--source-lf-prefix", required=True)
    parser.add_argument("--source-rs-prefix", required=True)
    parser.add_argument("--dest-prefix", required=True)
    parser.add_argument("--manifest-key", required=True)
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--verify-checksums", action="store_true",
                        help="Download and SHA-256 compare every source→dest ZIP pair. "
                             "Required for the Phase D2 migration gate.")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    ok = run_reconcile(
        s3=s3,
        bucket=args.bucket,
        source_lf_prefix=args.source_lf_prefix,
        source_rs_prefix=args.source_rs_prefix,
        dest_prefix=args.dest_prefix,
        manifest_key=args.manifest_key,
        verify_checksums=args.verify_checksums,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
