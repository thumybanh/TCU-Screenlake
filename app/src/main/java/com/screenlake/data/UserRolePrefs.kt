package com.screenlake.data

import android.content.Context
import android.content.SharedPreferences

/**
 * Lightweight role storage. Role is fetched from Cognito's `custom:tcu_role`
 * attribute and cached here so upload code can route S3 prefix without an
 * extra Cognito call per upload. Kept out of the Room `UserEntity` on purpose:
 * the Room DB uses `fallbackToDestructiveMigration()`, so adding a column
 * would wipe local unuploaded screenshots on the next app update.
 */
object UserRolePrefs {
    private const val PREFS_NAME = "user_role_prefs"
    private const val KEY_ROLE = "tcu_role"

    const val ROLE_TCU = "TCU"
    const val ROLE_TEST = "TEST"

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun setRole(context: Context, role: String?) {
        val normalized = normalize(role)
        prefs(context).edit().putString(KEY_ROLE, normalized).apply()
    }

    /** Returns TCU as the safe default when the role has not been synced yet. */
    fun getRole(context: Context): String {
        val raw = prefs(context).getString(KEY_ROLE, null)
        return normalize(raw)
    }

    fun isTest(context: Context): Boolean = getRole(context) == ROLE_TEST

    /** Build the badge/profile display string: `TCU-1234` or `TEST-0042`.
     * The prefix mirrors the DB role key so researchers can grep/filter the
     * same string in logs and S3 paths. */
    fun formatDisplayCode(code: String?, role: String): String {
        if (code.isNullOrBlank()) return "$role-????"
        return "$role-$code"
    }

    /** Human-facing label for the profile screen. The internal role key is
     * TCU/TEST (ugly), but participants and researchers should see
     * "Participant" / "Researcher" instead. */
    fun displayRole(role: String): String = when (role) {
        ROLE_TEST -> "Researcher"
        else -> "Participant"
    }

    private fun normalize(role: String?): String {
        val upper = role?.trim()?.uppercase()
        return if (upper == ROLE_TEST) ROLE_TEST else ROLE_TCU
    }
}
