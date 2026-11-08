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

## AWS migration + fixes (2026-10-05 to 2026-10-12)

### Problems hit and fixes

1. **Build failure "25"** — Gradle 8.9 incompatible with JDK 25. Fix: install JDK 21 via `brew install --cask temurin@21`, `export JAVA_HOME=$(/usr/libexec/java_home -v 21)`.
2. **`local.properties` ignored** — two files exist (`local.properties` + `app/local.properties`). Gradle reads root one only. Fix: edit root `local.properties`, not `app/`.
3. **Spaces around `=` in `local.properties`** — Java Properties parser treats leading space as part of value. Fix: no spaces around `=`.
4. **Lambda `assign_tcu_code` role propagation delay** — "role cannot be assumed by Lambda". Fix: `sleep 10` before `create-function`.
5. **PostConfirmation Lambda event shape** — Lambda expected `{cognito_sub, cognito_username}` but Cognito sends `event.request.userAttributes.sub` and `event.userName`. Fix: added Cognito trigger shape detection in `handler.py`.
6. **`admin-update-user-attributes` InvalidParameterException** — `custom:tcu_code` attribute missing from new user pool schema. Fix: `aws cognito-idp add-custom-attributes` to add it (one-way op, cannot delete).
7. **ScreenshotService not running** — recording stopped after login. Fix: open app, tap big record button, grant MediaProjection permission.
8. **UploadWorker dropped 4 orphan zips (18 MB each)** — zips survived on disk after uninstall but DB wiped. App cleaned them on restart since not in DB. Lost Oct 10 data (~603 shots).
9. **Workers scheduled RUNNABLE but idle** — WorkManager periodic jobs #35, #37 waited for next hourly tick. Fix: hit Upload button in app Stats screen OR force-fire with `adb shell cmd jobscheduler run -f com.screenlake <id>`.
10. **Phone-side pipeline fully alive after fresh login** — fresh Cognito tokens + manual Upload button triggered zip creation, upload, Lambda extraction, manifest writing. All verified.

### Upload speed optimization (shipped)

- `WorkerStarter.kt` — periodic 1h → 30 min, policy `KEEP` → `UPDATE`.
- `ZipFileWorker.kt` — chains `OneTimeWorkRequest<UploadWorker>` on success.
- Net effect: max end-to-end delay ~30 min (was ~2h). No manual button needed. 200-shot cap stays.

---

## OCR model research (2026-10-12)

### Why considering a swap

Current = Tesseract 4 (via `tesseract4android`), LSTM-only engine mode. Baseline accuracy on app screenshots ~70–85%. Status bar + UI chrome produces garbage text. Tiny fonts (captions, hashtags on images) often missed.

For the tobacco study specifically: keyword matching on main content text works OK with Tesseract. Theme classification for FDA warning labels / small-print disclaimers would suffer.

### Pilot methodology

- 20 real screenshots from `s3://tcu-bucket/academia/tenant/.../1001/*.zip` (YouTube Shorts content)
- Ran 4 engines on same JPGs, side-by-side comparison in `/tmp/ocr_pilot/comparison4.csv`
- Measured: char count, speed, text quality (status bar handling, punctuation, main content accuracy)

### Comparison table

| OCR Engine | Monthly cost (60 users × 168K shots) | Accuracy | Avg chars extracted | Avg speed (ms/shot) | Status bar handling | Punctuation | On-device viable |
|------------|------|----------|---------------------|---------------------|---------------------|-------------|------------------|
| Tesseract 4 (current Android) | $0 | ~70–80% | ~334 | ~500–1000ms | Garbage ("n2eEBge-") | Weak | Yes (now) |
| Tesseract 5 desktop | $0 | ~75–82% | 334 | 607ms | Garbage ("131 eege-") | Weak | Not applicable |
| PaddleOCR PP-OCRv6 | $0 | ~90–93% | 346 | 7,894ms | Clean (filters UI) | Better | **No** (18× too slow) |
| **RapidOCR (PP-OCR ONNX)** | **$0** | **~85–90%** | **356** | **435ms** | **Clean** ("11:31") | OK | **Yes** |
| Google Cloud Vision | ~$10,500 | ~95%+ | n/a | ~200ms (API) | Clean | Excellent | Via API |
| Bedrock Mistral OCR | ~$6,000 | ~90–93% | n/a | ~500ms (API) | Clean | Good | Via API |
| AWS Textract | ~$15,000 | ~70–85% on UI | n/a | ~400ms (API) | Weak | OK | Via API |

### Why choosing RapidOCR

- **Free forever** (MIT license)
- **Same accuracy as PaddleOCR, 18× faster** — uses PP-OCRv4 ONNX quantized models
- **Clean status bar handling** — reads time correctly ("11:31" not "131 eege")
- **Mobile-grade speed** — 435ms/shot on Mac, expected ~500ms–1s on Pixel 7a. Fits 5-second capture interval
- **Has official Android port** — github.com/RapidAI/RapidOcrAndroidOnnx
- **Keeps data on-device** — no network OCR, no API quotas, no PII leaving device
- **No cloud dependency** — no Google billing, no AWS Bedrock, no API keys
- Only Google Vision beats it on raw accuracy but costs $10K+/month

### Rejected options

- **Tesseract 5 upgrade**: Tesseract 5 LSTM ≈ Tesseract 4 LSTM. Marginal gain (~3–5 pp), not worth 1 week dev effort.
- **PaddleOCR direct**: 7.9s/shot on Mac = unusable on-device. Would fall behind 60% per shot.
- **Google Vision**: $10,500/mo at study scale. Need to pilot first before committing.
- **AWS Textract**: trained on documents, poor on app UI. ~70–85% accuracy = no gain over Tesseract.
- **Rekognition DetectText**: 50-word hard cap per image. Disqualified for dense screenshots.

### Integration path (not yet started)

- Add ONNX Runtime Android SDK (`onnxruntime-android` Gradle dep)
- Download pretrained models (det + rec + cls, ~15 MB)
- Replace `Recognize.kt processImage()` to call RapidOCR
- Est effort: ~2–3 days

### Pilot files (local, not in repo)

- `/tmp/ocr_pilot/` — venv + comparison scripts (pilot2–4.py) + comparison CSVs
- Dependencies: `python3.11`, `paddleocr`, `rapidocr-onnxruntime`, `pytesseract`, `tesseract` (brew)

---

## 2026-11-07 / 2026-11-08 Session — Issues & Fixes

### Problems hit and fixes

1. **Build failure — Java 25 incompatible with Kotlin**
   Gradle failed with `IllegalArgumentException: 25`. Fix: `JAVA_HOME=/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home ./gradlew assembleDebug`.

2. **UploadWorker silently rejected LF/RS codes after ID migration**
   Regex `\d{4}` only matched legacy 4-digit codes. New `LF####` / `RS####` codes failed validation, leaving `emailHash` stale in Room DB forever — all uploads went to wrong S3 path.
   Fix: updated regex to accept `^LF[1-9]\d{3}$`, `^RS\d{4}$`, and legacy 4-digit. Also fixed a Kotlin null-safety crash on `code.matches()`.

3. **Cognito app client missing ReadAttributes — role and assigned_at never reached the app**
   `custom:tcu_role` and `custom:assigned_at` were not in the app client `ReadAttributes` list. Amplify silently dropped them on every `fetchUserAttributes()` call. The app never knew the user's role and always fell back to LF (default), meaning RS users uploaded to the wrong S3 prefix.
   Fix: added both attributes to `ReadAttributes` via `aws cognito-idp update-user-pool-client`.

4. **All researcher accounts had legacy role `TEST` instead of `RS`**
   All 6 researcher accounts had `custom:tcu_role = TEST` (name before the RS/LF rename). The app normalizes `TEST → RS` internally, but Cognito should store canonical values.
   Fix: bulk-updated all 6 accounts to `RS` via `admin-update-user-attributes`. Deleted `zenamy51@gmail.com` (no longer needed).

5. **"Signed in since" showed wrong date (Nov 8 instead of Oct 5)**
   Root causes (chain of issues):
   - `LoginFragment` created `UserEntity` with no timestamp on fresh install
   - `getOrSetFirstSeenDate()` cached today's date in SharedPreferences on first open
   - `custom:assigned_at` Cognito attribute didn't exist yet
   - Even after adding it, it wasn't readable (see issue #3 above)
   Fix: added `custom:assigned_at` to Cognito user pool schema; wrote Oct 5 epoch for existing accounts; updated Lambda to write it for all future accounts; app now reads and backfills `UserEntity.createdTimestamp` from it on record button tap; SharedPreferences cache cleared when backfill succeeds.

6. **Firebase App Distribution — wrong tester group name**
   `build.gradle.kts` defaulted to group `"participants"` which doesn't exist in Firebase. APK uploaded but was distributed to nobody.
   Fix: changed to `testers = "mythubanh@gmail.com"` for direct targeted distribution.

7. **Mystery gray number on recording screen**
   `tvUpdates` (uploadCountMsg) was visible at the bottom with no label — just a floating gray number.
   Fix: set `visibility="gone"` on `tvUpdates` and `tvUpdatesUpload`.

8. **Pending shots / pending zips showed `—` on home screen**
   LiveData only got posted during active recording. Values never appeared when just viewing the home screen.
   Fix: added DB query in `ScreenRecordFragment.onResume()` to post counts immediately on every screen open.

### New features added

- **Pending shots + pending zips on recording screen** — updates live during recording and on every resume
- **Last successful upload timestamp on Profile tab** — full `yyyy-MM-dd HH:mm:ss`
- **Bug report email notifications** — S3 → SNS → email. Created SNS topic `screenlake-bug-reports`, wired to `academia/reports/**/*.json` and `academia/testing/reports/**/*.json`. `mythubanh@gmail.com` subscribed. Setup script: `lambda/setup_report_notifications.sh`.

### AWS changes made

| Resource | Change |
|----------|--------|
| Cognito user pool | Added `custom:assigned_at` to schema |
| Cognito app client | Added `custom:tcu_role`, `custom:assigned_at` to ReadAttributes |
| Cognito users (5) | Updated `custom:tcu_role` TEST → RS (mythubanh, jgcordova07, jessie, jacksonberger518, kclockton) |
| Cognito user | Deleted `zenamy51@gmail.com` |
| Cognito user (mythubanh) | Set `email_verified = true`, wrote `custom:assigned_at = 1791177470` (Oct 5) |
| SNS | Created topic `screenlake-bug-reports`, subscribed `mythubanh@gmail.com` |
| S3 (tcu-bucket) | Added two SNS event rules for report JSON files |

### App versions released

| Version | Build | Key changes |
|---------|-------|-------------|
| 1.40 | 48 | Pending stats on home, mystery number hidden, profile cleanup |
| 1.41 | 49 | Signed-in-since backfill from Cognito (condition bug — skipped existing records) |
| 1.42 | 50 | Fixed backfill — always overwrites from Cognito `assigned_at` |

### Still deferred

- **Jessie migration** — rename code `1002` → `RS0001`. Role updated to RS but code and S3 data still under `1002`. 14-step plan from prior session still applies.
- **Deploy updated `assign_tcu_code` Lambda** — has `custom:assigned_at` write added this session, not yet deployed to AWS.

---

## Changelog (my edits)

<!-- add entries as I ship changes -->
- 2026-10-01: Created this notes file.
- 2026-10-05: AWS migration to new TCU account (951662621099). New bucket `tcu-bucket`, new Cognito pool `us-east-2_5pvOLMS20`, new identity pool, redeployed both Lambdas, custom attribute `tcu_code` added to pool schema.
- 2026-10-05: Rebranded SRK → TCU Screenlake. Primary color #4d1979. Logo replaced in all mipmap densities + drawables. `:ENTER YOUR UNIVERSITY HERE:` → Texas Christian University.
- 2026-10-05: Added `TCU-` prefix to participant code input (`app:prefixText="TCU-"` in layout). Max 4 digits, numeric input only.
- 2026-10-12: Lost Oct 10 data (~603 shots in 4 orphan zips). Zips survived uninstall on disk but DB wiped. App cleaned them on restart since not in DB.
- 2026-10-12: Upload speed optimization — periodic 1h → 30 min, chained UploadWorker off ZipFileWorker. Max end-to-end delay ~30 min.
- 2026-10-12: OCR pilot run. RapidOCR chosen as next OCR. Integration not yet started.
