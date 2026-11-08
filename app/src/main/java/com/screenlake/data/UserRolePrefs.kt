package com.screenlake.data

import android.content.Context
import android.content.SharedPreferences

/**
 * Lightweight role storage. Role is fetched from Cognito's `custom:tcu_role`
 * attribute and cached here so upload code can route S3 prefix without an
 * extra Cognito call per upload. Kept out of the Room `UserEntity` on purpose:
 * the Room DB uses `fallbackToDestructiveMigration()`, so adding a column
 * would wipe local unuploaded screenshots on the next app update.
 *
 * Role constants (stored in Cognito + SharedPrefs):
 *   ROLE_LF  — Leap Forward participant (cohort-based, e.g. LF14827)
 *   ROLE_RS  — Research Staff (e.g. RS001)
 *
 * Legacy values "TCU" and "TEST" are normalized on read for backward compat
 * with accounts created before the rename.
 */
object UserRolePrefs {
    private const val PREFS_NAME = "user_role_prefs"
    private const val KEY_ROLE = "tcu_role"

    const val ROLE_LF = "LF"
    const val ROLE_RS = "RS"

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun setRole(context: Context, role: String?) {
        val normalized = normalize(role)
        prefs(context).edit().putString(KEY_ROLE, normalized).apply()
    }

    /** Returns ROLE_LF as the safe default when the role has not been synced yet. */
    fun getRole(context: Context): String {
        val raw = prefs(context).getString(KEY_ROLE, null)
        return normalize(raw)
    }

    fun isResearcher(context: Context): Boolean = getRole(context) == ROLE_RS

    /** Alias kept so existing callers (RealUploadHandler) compile without changes. */
    fun isTest(context: Context): Boolean = isResearcher(context)

    /**
     * Display the participant/researcher ID in the badge and profile screen.
     * New codes already contain the prefix (LF14827, RS001), so we return the
     * code as-is. Legacy 4-digit numeric codes fall back to "$role-$code".
     */
    fun formatDisplayCode(code: String?, role: String): String {
        if (code.isNullOrBlank()) return "$role-????"
        val isLegacyNumeric = code.all { it.isDigit() }
        return if (isLegacyNumeric) "$role-$code" else code
    }

    fun displayRole(role: String): String = when (role) {
        ROLE_RS -> "Researcher"
        else -> "Participant"
    }

    private fun normalize(role: String?): String {
        return when (role?.trim()?.uppercase()) {
            ROLE_RS, "TEST" -> ROLE_RS   // TEST = legacy researcher role
            else -> ROLE_LF              // TCU, LF, null, anything else = participant
        }
    }
}
