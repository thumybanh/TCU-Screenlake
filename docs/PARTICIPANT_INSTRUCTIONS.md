# Screenlake App — Participant Setup Guide

Welcome, and thank you for joining the TCU Screenlake study. This guide walks you through installing the app, signing in, and setting up your phone so your participation can begin.

Expected time: **10–15 minutes.**

If you get stuck at any step, screenshot the problem and email the researcher.

---

## What you need before you start

- An Android phone (version 10 or newer).
- The email address and 4-digit participation code the researcher sent you.
- A stable WiFi connection.
- About 100 MB of free storage.

---

## Step 1 — Download the app

1. On your phone, open the **OneDrive link** the researcher emailed you.
2. Tap the file `app-debug.apk` to download it. (Should take 10–30 seconds.)
3. When download finishes, tap the file to open it.

---

## Step 2 — Install the app

Android will show a warning: _"For your security, your phone is not allowed to install unknown apps from this source."_

1. Tap **Settings** on the warning.
2. Enable the toggle **Allow from this source**.
3. Tap the back button, then tap **Install** on the install screen.
4. Wait for install to finish (10–20 seconds).
5. Tap **Open** when done.

If Google Play Protect shows a warning ("App not scanned" or "Unsafe app"), tap **Install anyway** — this is normal for research apps that aren't distributed through the Play Store.

---

## Step 3 — Sign up

1. On the sign-up screen, enter:
   - **Email:** the exact email the researcher used to register you.
   - **Participation code:** the 4-digit code the researcher sent you.
2. Check the two agreement boxes.
3. Tap **Next**.

If you see an error:
- _"The participation code does not match this email"_ — the code is wrong. Check the email from your researcher.
- _"This email is not on the participant list"_ — the email is wrong. Make sure you entered exactly what the researcher used (watch for typos, extra dots, wrong domain).

4. On the next screen, create a password:
   - At least 8 characters.
   - Must include an uppercase letter, a lowercase letter, a number, and a symbol.
5. Confirm the password and tap **Register**.

---

## Step 4 — Verify your email

Within 1–2 minutes, you'll receive a 6-digit verification code at the email you signed up with.

1. Open your email inbox.
2. Look for a message from **AWS / Cognito / no-reply**.
3. **Check your spam / junk folder** if you don't see it in the inbox — this is common.
4. Enter the 6-digit code in the app.
5. Tap **Confirm**.

If the code never arrives after 5+ minutes, email the researcher — they can resend it.

---

## Step 5 — Grant required permissions

This step is critical. If any permission is missing, the app will NOT capture any data, even if signup worked.

The app will prompt you through each permission. Follow the on-screen instructions carefully.

### 5a. Accessibility permission (most important)

The app needs this to read screen content for the study.

1. The app opens **Settings → Accessibility**.
2. Scroll until you see **Screenlake** (or **TCU Screenlake**).
3. Tap it, then toggle it **ON**.
4. Confirm the warning dialog.

#### If you see "Restricted settings" message

On newer Android phones (Android 13+, including Google Pixel), Accessibility is blocked by default for sideloaded apps.

**To unblock:**

1. Go back to the main **Settings** app.
2. Tap **Apps** → **See all apps**.
3. Find and tap **Screenlake**.
4. Tap the **⋮ three-dot menu** in the top right corner.
5. Tap **Allow restricted settings**.
6. Confirm the warning.
7. Go back to **Settings → Accessibility → Screenlake** and toggle it **ON**.

On Samsung phones: the "Allow restricted settings" option is at the bottom of the Screenlake App Info page, not under a three-dot menu.

### 5b. Usage access permission

Needed so the app knows which app you're currently using.

1. The app opens **Settings → Special access → Usage access** (or similar).
2. Tap **Screenlake**.
3. Toggle **Permit usage access** ON.

### 5c. Battery optimization exemption

Keeps the service running in the background.

1. Open **Settings → Apps → Screenlake → Battery** (or **Battery usage**).
2. Set it to **Unrestricted** (or disable battery optimization for Screenlake).

Without this, your phone will kill the app in the background and data collection will stop silently.

### 5d. Notification permission (Android 13+)

Required so the app can show a persistent notification while running.

1. When prompted, tap **Allow**.

---

## Step 6 — You're done

You should now see the main app screen showing **"Recording"** or an active status indicator.

Your phone will capture a screenshot every 5 seconds while you use it. You can continue using your phone normally — the app runs in the background.

### What happens next (automatic)

- The app captures screenshots to local storage.
- Every hour, screenshots are packaged into a zip file.
- The zip uploads to the research cloud when you're on **WiFi** AND **charging**.
- Nothing uploads over cellular data. Nothing uploads if your phone is unplugged.

To make sure data flows, **plug your phone in while on WiFi** at least once per day (overnight charging is ideal).

---

## Troubleshooting

### "No data has been uploaded" after a day

Check these in order:

1. Open the app — does it say Recording / Active?
2. **Settings → Accessibility → Screenlake** — is it ON?
3. **Settings → Apps → Screenlake → Battery** — is it Unrestricted?
4. **Settings → Special access → Usage access → Screenlake** — is it enabled?
5. Have you charged your phone on WiFi recently? Both are required for upload.

If all four are OK and still no data after another 24 hours, email the researcher.

### The app crashed or shows an error

Screenshot the error screen and email the researcher.

### I accidentally revoked a permission

Go back to the Settings path for that permission and re-enable it. The app will resume capture within a few minutes.

### I changed my mind — how do I stop?

Open the app, go to Settings within the app, and tap **Stop Recording** or **Delete Account**. Alternatively, email the researcher and they'll remove your account.

### My phone restarted — do I need to re-enable anything?

Permissions survive reboots. The service auto-starts on boot. No action needed.

---

## Privacy summary

- Only screenshots and metadata are captured. No microphone, no camera, no location, no contacts.
- Captures are encrypted on your device and transmitted over HTTPS to the TCU research cloud.
- You can opt out at any time by contacting the researcher.
- All data handling follows the IRB-approved study protocol you were briefed on.

---

## Need help?

Email: **[researcher's email]**

Include:
- Your participant code.
- What you were trying to do.
- A screenshot if there was an error on screen.
