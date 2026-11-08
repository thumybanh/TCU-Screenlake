#!/usr/bin/env python3
"""Controlled backfill of historical researcher ZIPs through the unpack Lambda.

Processes each ZIP at the RS0006 destination prefix synchronously (one at a time).
After each invocation it reads the manifest and verifies:
  - The ZIP UUID appears in _processed_zip_uuids
  - total_screenshots increased by the expected JPG count from the local ZIP

Stops immediately on any failure. Does not proceed to the next ZIP until the
current one is verified. Relies on reserved concurrency = 1 being set on the
unpack Lambda before this script runs (see Phase B2).

Usage:
  python3 backfill.py \
    --bucket <bucket-name> \
    --prefix "academia/testing/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/RS0006/" \
    --manifest-key "academia/testing/_manifests/panel=general_1/panelist=RS0006/_index.json" \
    --function screenlake-unpack-zips \
    --region us-east-2 \
    [--dry-run] [--resume]

Flags:
  --dry-run   List ZIPs and compute JPG counts without invoking the Lambda.
  --resume    Skip ZIPs whose UUID is already in the manifest (safe restart
              after a failed or interrupted run).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
import tempfile
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError


def count_jpgs_in_zip(local_path: str) -> int:
    """Return the number of .jpg entries inside a local ZIP file."""
    with zipfile.ZipFile(local_path) as zf:
        return sum(1 for n in zf.namelist() if n.lower().endswith(".jpg"))


def load_manifest(s3, bucket: str, manifest_key: str) -> dict:
    """Load the RS0006 manifest JSON. Returns empty manifest if the key is absent."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=manifest_key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return {"_processed_zip_uuids": [], "total_screenshots": 0}
        raise


def build_s3_event(bucket: str, key: str) -> dict:
    """Synthetic S3 ObjectCreated event for a single object."""
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": quote(key, safe="/")},
                }
            }
        ]
    }


def run_backfill(
    s3,
    lam,
    bucket: str,
    prefix: str,
    manifest_key: str,
    function_name: str,
    dry_run: bool = False,
    resume: bool = False,
) -> bool:
    """Execute the backfill loop. Returns True on success, False on any failure.

    Separated from main() so tests can inject mock clients directly.
    """
    # --- List all ZIPs at the destination prefix ---
    print(f"Listing ZIPs at s3://{bucket}/{prefix}")
    zip_keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".zip"):
                zip_keys.append(key)

    if not zip_keys:
        print("ERROR: no ZIPs found at prefix. Verify the path and re-run.")
        return False

    print(f"Found {len(zip_keys)} ZIPs.")

    # --- Load current manifest ---
    manifest = load_manifest(s3, bucket, manifest_key)
    already_processed: set[str] = set(manifest.get("_processed_zip_uuids", []))
    print(f"Manifest currently contains {len(already_processed)} processed UUIDs.")

    # --- Pre-compute expected JPG counts ---
    print("\nPre-computing JPG counts (downloading ZIPs locally)...")
    zip_jpg_counts: dict[str, int] = {}
    for key in zip_keys:
        zip_name = os.path.basename(key)
        zip_uuid = os.path.splitext(zip_name)[0]
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            s3.download_file(bucket, key, tmp_path)
            count = count_jpgs_in_zip(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        zip_jpg_counts[zip_uuid] = count
        print(f"  {zip_name}: {count} JPGs")

    total_expected = sum(zip_jpg_counts.values())
    print(f"\nTotal expected screenshots across all ZIPs: {total_expected}")

    if dry_run:
        print("\n[dry-run] No Lambda invocations performed.")
        return True

    # --- Process each ZIP synchronously ---
    errors: list[str] = []
    skipped = 0
    processed = 0

    for key in zip_keys:
        zip_name = os.path.basename(key)
        zip_uuid = os.path.splitext(zip_name)[0]

        was_already_processed = zip_uuid in already_processed

        if resume and was_already_processed:
            print(f"[skip] {zip_name}: already in manifest")
            skipped += 1
            continue

        print(f"\n[invoke] {zip_name}")
        event_payload = build_s3_event(bucket, key)

        try:
            response = lam.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(event_payload).encode("utf-8"),
            )
        except ClientError as e:
            msg = f"Lambda invoke failed for {zip_name}: {e}"
            print(f"ERROR: {msg}")
            errors.append(msg)
            break

        if response.get("FunctionError"):
            payload_bytes = response["Payload"].read()
            msg = (f"Lambda returned FunctionError for {zip_name}: "
                   f"{payload_bytes.decode('utf-8', errors='replace')}")
            print(f"ERROR: {msg}")
            errors.append(msg)
            break

        raw_payload = response["Payload"].read()
        try:
            result = json.loads(raw_payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            msg = f"Could not parse Lambda response for {zip_name}: {e}"
            print(f"ERROR: {msg}")
            errors.append(msg)
            break

        if "processed" not in result:
            msg = f"Unexpected Lambda response for {zip_name}: {result}"
            print(f"ERROR: {msg}")
            errors.append(msg)
            break

        # Re-read manifest to verify this ZIP was recorded
        old_total = manifest.get("total_screenshots", 0)
        updated_manifest = load_manifest(s3, bucket, manifest_key)
        updated_uuids = set(updated_manifest.get("_processed_zip_uuids", []))

        if zip_uuid not in updated_uuids:
            msg = (
                f"ZIP UUID '{zip_uuid}' missing from manifest after invocation. "
                "Lambda may have skipped processing or failed before writing the manifest."
            )
            print(f"ERROR: {msg}")
            errors.append(msg)
            break

        new_total = updated_manifest.get("total_screenshots", 0)
        delta = new_total - old_total
        expected_delta = zip_jpg_counts[zip_uuid]

        if delta != expected_delta:
            # If this ZIP was already in the manifest before this run, a delta of 0
            # is the Lambda's idempotency skip — expected and not an error.
            if was_already_processed and delta == 0:
                print(f"  OK: already in manifest before this run; Lambda idempotency skip, delta=0 expected")
            else:
                msg = (
                    f"Screenshot count delta for {zip_name}: "
                    f"expected +{expected_delta}, got +{delta} "
                    f"(manifest total: {old_total} → {new_total})"
                )
                print(f"WARNING: {msg}")
                if expected_delta > 0:
                    errors.append(msg)
                    break

        print(
            f"  OK: UUID in manifest, screenshots +{delta} "
            f"(manifest total now {new_total})"
        )
        manifest = updated_manifest
        processed += 1

    # --- Final summary ---
    print(f"\n{'='*60}")
    print(f"Processed: {processed}  Skipped: {skipped}  Errors: {len(errors)}")

    if errors:
        print(f"\nBackfill STOPPED due to errors:")
        for e in errors:
            print(f"  - {e}")
        return False

    final_manifest = load_manifest(s3, bucket, manifest_key)
    final_uuids = set(final_manifest.get("_processed_zip_uuids", []))
    final_total = final_manifest.get("total_screenshots", 0)
    all_zip_uuids = set(zip_jpg_counts.keys())
    missing_from_manifest = all_zip_uuids - final_uuids
    duplicate_count = len(final_manifest.get("_processed_zip_uuids", [])) - len(final_uuids)

    print(f"\nFinal manifest state:")
    print(f"  total_screenshots   : {final_total}  (expected {total_expected})")
    print(f"  _processed_zip_uuids: {len(final_uuids)} unique UUIDs")
    print(f"  Missing from manifest: {len(missing_from_manifest)}")
    print(f"  Duplicate UUIDs     : {duplicate_count}")

    ok = True
    if final_total != total_expected:
        print(f"MISMATCH: total_screenshots {final_total} != expected {total_expected}")
        ok = False
    if missing_from_manifest:
        print(f"MISSING from manifest: {sorted(missing_from_manifest)}")
        ok = False
    if duplicate_count:
        print("DUPLICATE UUIDs in manifest: investigate _processed_zip_uuids list")
        ok = False

    if ok:
        print("\nBackfill complete. All ZIPs verified.")
    else:
        print("\nBackfill finished with discrepancies. Investigate before proceeding.")

    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--manifest-key", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    lam = boto3.client("lambda", region_name=args.region)

    ok = run_backfill(
        s3=s3,
        lam=lam,
        bucket=args.bucket,
        prefix=args.prefix,
        manifest_key=args.manifest_key,
        function_name=args.function,
        dry_run=args.dry_run,
        resume=args.resume,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
