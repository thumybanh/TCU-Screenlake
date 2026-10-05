# My Working Notes

Personal running log. Not official docs. Track open questions, decisions, and planned changes.

---

## Upload timing (how data gets to S3)

### Current flow (no manual trigger)

1. Phone captures screenshots to local DB. Rows marked `isOcrComplete=false`.
2. OCR runs when: screen locks, or `ZipFileWorker` periodic tick calls `runPendingOcr`. Auto-trigger in `ScreenshotService.beginOcr` also fires when batch ≥ 5 AND last OCR > 2h ago.
3. `ZipFileWorker` runs every **1 hour** (periodic). Zips any OCR-complete rows. No minimum count — 20 shots still becomes 1 zip.
4. `UploadWorker` runs every **1 hour** (periodic, separate schedule). Uploads whatever zips are queued.
5. Lambda `screenlake-unpack-zips` fires on S3 ObjectCreated. Extracts 5 CSVs into `data/` tree. Rewrites `_manifests/_index.json`.

Worst-case end-to-end delay: up to ~2h after last screenshot (zip tick + upload tick). Observed: ~1h for 20 screenshots when workers happen to align.

### Manual button path

Chains `ZipFileWorker` → `UploadWorker` as `OneTimeWorkRequest` back-to-back (`ScreenshotService.kt:970-982`). No hourly wait. Still gated by `uploadOverWifi` / `uploadOverPower` settings. OCR runs first (per-image timeout 30s, typical 0.5–3s on modern phone).

### How to make default faster (not yet done)

- **Shorten periodic interval to 15 min** (Android floor): `WorkerStarter.kt:59,74` — `1, TimeUnit.HOURS` → `15, TimeUnit.MINUTES`. Also change `ExistingPeriodicWorkPolicy.KEEP` → `UPDATE`.
- **Chain UploadWorker off ZipFileWorker success**: in `ZipFileWorker.doWork()`, after `zipUpScreenshots(200)` succeeds, enqueue `OneTimeWorkRequest<UploadWorker>`. Removes upload's independent wait.
- **Event-driven zip trigger** on OCR complete: when `getZippableScreenshotCount()` ≥ 50, enqueue one-time `ZipFileWorker`.

Recommended combo: interval change + chain-on-success. Cuts worst case to ~15–20 min.

### Timing estimates (200 screenshots, modern phone)

- OCR (Tesseract, sequential, chunked 50): ~5 min typical, 10–15 min slow phone.
- Zip: ~5–10s.
- Upload (~30–80 MB zip): WiFi 10 Mbps ~1 min, 20 Mbps ~30s.
- Lambda unpack: cold ~3–10s, warm ~1–3s.
- End-to-end with manual button: **~6–15 min** typical.

---

## S3 bucket layout (current)

Single bucket `my-tcu-bucket`. Three prefixes:

- `academia/tenant/{tenantId}/panel/{panelId}/panelist/{userId}/{uuid}.zip` — raw zip, immutable. Up to 200 JPGs + 5 CSVs each.
- `academia/log_events_v2/{emailHash}/{buildVersion}/{uuid}.csv` — app diagnostic logs.
- `data/panel=.../panelist=.../date=.../*.csv` — Lambda-extracted 5 CSVs per zip. Hive-partitioned, Athena-queryable.
- `_manifests/panel=.../panelist=.../_index.json` — rolling per-participant summary. Only file in bucket that's rewritten in place.

Every zip always has **5 CSVs regardless of screenshot count** (may be header-only if that kind had no rows). JPGs stay zipped, never extracted individually.

### Known duplications

- Metadata CSVs exist both inside zip (`academia/`) and extracted (`data/`). Intentional: raw audit vs queryable. Tiny cost impact.
- `log_data_*.csv` inside zip duplicates loose CSVs in `academia/log_events_v2/`. ~2–5% zip bloat. Candidate to drop (see `NEXT_STEPS.md:73`).

---

## Storage cost math for solution architect

- Per participant per day: ~5,000–10,000 screenshots × ~300 KB = **1.5–3 GB/day**.
- 100 participants × 30 days ≈ **4.5–9 TB per study**.
- 99% of volume = JPGs. Metadata CSVs negligible.

### Cost levers to discuss

1. S3 lifecycle policy: raw zips > 90 days → **Glacier Instant Retrieval** (~$4/TB/month vs ~$23/TB/month standard). Retrieval still ms latency.
2. Convert extracted CSVs to **Parquet** via Athena CTAS. ~10× smaller, faster queries.
3. Intelligent-Tiering for first 90 days.
4. Lambda + Athena request costs.
5. KMS encryption, cross-region replication for compliance.

---

## AWS services currently used

- **S3** — the bucket.
- **Lambda** — `screenlake-unpack-zips` (zip extractor), `assign_tcu_code` (participant ID assignment).
- **Cognito** — auth, TCU code as custom attribute.
- **DynamoDB** — TCU code tracking.
- **Amplify** — Android SDK client to Cognito + S3.
- **IAM** — Lambda roles.

Not used (yet): Athena (ready but no tables created), Rekognition, Comprehend, Bedrock, SageMaker.

---

## AWS AI options for analysis (future)

### For OCR text (CSV data)

- **Comprehend PII** — auto-flag SSN/credit card/email. ~90%+ on regex-backed types. Good first-pass redaction.
- **Bedrock Claude text** — qualitative coding, structured JSON extraction. ~90–95% with good prompts.
- Skip **Comprehend Sentiment** — too noisy on short OCR fragments.

### For screenshot images (JPGs)

- **Bedrock Claude vision (Haiku)** — cheap first pass, ~$8–15 per 10K images. Describes app + activity + content.
- **Bedrock Claude vision (Sonnet)** — deeper analysis on sampled subset. ~$30–80 per 10K.
- **Rekognition Content Moderation** — NSFW/violence flag. Cheap safety net ($0.001/image).
- **Skip Rekognition Labels** — trained on real-world photos, returns useless labels (Text, Screen, Electronics) for app UI.

### Why Rekognition Labels fails for screenshots

Trained on photos (dogs, cars, people), not app UI. Returns "Screen / Text / Electronics" — can't identify app, feature, or user activity. For behavioral research, need LLM vision.

### Rule of thumb

Photo of real world → Rekognition. Photo of a screen → Bedrock Claude vision.

### Pilot plan before scaling

Sample 100–500 screenshots. Run Haiku vision with fixed prompt. Compare output to research questions. Scale if ≥80% coverage.

### Cost warning

10K shots/day × 100 users × $0.001/image = $1K/day. Sample, don't blanket-run. Ask architect about Bedrock provisioned throughput at study scale.

---

## Open questions

- [ ] Should default upload interval drop from 1h to 15 min?
- [ ] Chain UploadWorker off ZipFileWorker success?
- [ ] When to create Athena tables (DDL for 5 kinds)?
- [ ] Convert `data/` CSVs to Parquet?
- [ ] Lifecycle policy for `academia/` zips?
- [ ] PII redaction pipeline — Comprehend PII on OCR text?
- [ ] Bedrock vision pilot — which 100 screenshots to use?

---

## Changelog (my edits)

<!-- add entries as I ship changes -->
- 2026-10-01: Created this notes file.
