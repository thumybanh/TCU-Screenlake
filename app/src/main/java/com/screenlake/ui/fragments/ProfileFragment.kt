package com.screenlake.ui.fragments

import android.os.Bundle
import android.text.InputType
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.findNavController
import com.amplifyframework.core.Amplify
import com.screenlake.BuildConfig
import com.screenlake.R
import com.screenlake.data.UserRolePrefs
import com.screenlake.data.database.dao.ScreenshotDao
import com.screenlake.data.database.dao.ScreenshotZipDao
import com.screenlake.data.database.dao.UserDao
import com.screenlake.data.repository.AwsService
import com.screenlake.recorder.services.ScreenshotService
import com.screenlake.recorder.upload.Util
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import timber.log.Timber
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import javax.inject.Inject

@AndroidEntryPoint
class ProfileFragment : Fragment() {

    @Inject lateinit var userDao: UserDao
    @Inject lateinit var screenshotDao: ScreenshotDao
    @Inject lateinit var screenshotZipDao: ScreenshotZipDao
    @Inject lateinit var awsService: AwsService

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?
    ): View = inflater.inflate(R.layout.fragment_profile, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        loadProfile(view)

        view.findViewById<Button>(R.id.profileReportIssueButton).setOnClickListener {
            showReportIssueDialog()
        }

        view.findViewById<Button>(R.id.profileSignOutButton).setOnClickListener {
            signOut()
        }
    }

    override fun onResume() {
        super.onResume()
        view?.let { loadProfile(it) }
    }

    private fun loadProfile(root: View) {
        lifecycleScope.launch {
            val user = withContext(Dispatchers.IO) { userDao.getUser() }
            val pendingShots = withContext(Dispatchers.IO) {
                runCatching { screenshotDao.getTotalCount() }.getOrDefault(0)
            }
            val pendingZips = withContext(Dispatchers.IO) {
                runCatching { screenshotZipDao.getCount() }.getOrDefault(0)
            }

            val ctx = context?.applicationContext ?: return@launch
            val role = UserRolePrefs.getRole(ctx)
            val code = user?.emailHash

            root.findViewById<TextView>(R.id.profileParticipantId).text =
                UserRolePrefs.formatDisplayCode(code, role)
            root.findViewById<TextView>(R.id.profileRole).text = UserRolePrefs.displayRole(role)
            root.findViewById<TextView>(R.id.profileEmail).text = user?.email ?: "unknown"
            root.findViewById<TextView>(R.id.profileDevice).text =
                listOfNotNull(user?.model, user?.product).joinToString(" / ").ifBlank { "unknown" }
            root.findViewById<TextView>(R.id.profilePendingCount).text = pendingShots.toString()
            root.findViewById<TextView>(R.id.profilePendingZips).text = pendingZips.toString()
            root.findViewById<TextView>(R.id.profileSignedInSince).text =
                user?.createdAt ?: user?.createdTimestamp ?: "unknown"
            root.findViewById<TextView>(R.id.profileLastUpload).text = formatLastUpload()
            root.findViewById<TextView>(R.id.profileAppVersion).text =
                "${BuildConfig.VERSION_NAME} (build ${BuildConfig.VERSION_CODE})"
        }
    }

    private fun formatLastUpload(): String {
        val millis = ScreenshotService.lastUploadTime.value ?: 0L
        if (millis <= 0L) return "never"
        return SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date(millis))
    }

    /** Opens an inline dialog with a text area + submit button. On submit, the
     * issue report is written as JSON to S3 under `reports/{date}/{code}/`
     * so the researcher can sync them all down with one `aws s3 sync`. */
    private fun showReportIssueDialog() {
        val ctx = context ?: return
        val input = EditText(ctx).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 4
            maxLines = 10
            hint = "What went wrong? Include steps if you can."
        }
        val container = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 0)
            addView(input)
        }

        AlertDialog.Builder(ctx)
            .setTitle("Report an issue")
            .setView(container)
            .setPositiveButton("Submit") { _, _ ->
                val text = input.text?.toString()?.trim().orEmpty()
                if (text.isBlank()) {
                    Toast.makeText(ctx, "Please describe the issue.", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                submitReport(text)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun submitReport(description: String) {
        val ctx = context?.applicationContext ?: return
        Toast.makeText(ctx, "Submitting report...", Toast.LENGTH_SHORT).show()

        lifecycleScope.launch {
            val ok = withContext(Dispatchers.IO) {
                runCatching { uploadReport(description) }.getOrDefault(false)
            }
            Toast.makeText(
                ctx,
                if (ok) "Report submitted. Thank you!" else "Could not submit — check your connection and try again.",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    private suspend fun uploadReport(description: String): Boolean {
        val ctx = context?.applicationContext ?: return false
        val user = withContext(Dispatchers.IO) { userDao.getUser() }
        val role = UserRolePrefs.getRole(ctx)
        val code = user?.emailHash ?: "unknown"

        val report = JSONObject().apply {
            put("participant_code", UserRolePrefs.formatDisplayCode(code, role))
            put("role", role)
            put("email", user?.email ?: "")
            put("device", "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}")
            put("android_version", android.os.Build.VERSION.RELEASE)
            put("app_version", "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
            put("submitted_at_epoch_ms", System.currentTimeMillis())
            put("submitted_at", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US).format(Date()))
            put("description", description)
        }

        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        val uuid = UUID.randomUUID().toString()
        val uploadPath = "reports/$today/${role}-${code}/$uuid.json"

        // Write to a temp file because the existing S3 presign helper looks
        // up MIME type from the file extension. Delete after the PUT completes.
        val tmpFile = File(ctx.cacheDir, "report-$uuid.json")
        tmpFile.writeText(report.toString())

        return try {
            val presignedUrl = Util().generates3ShareUrl(ctx, tmpFile.path, uploadPath)
            if (presignedUrl.isNullOrBlank()) {
                Timber.tag("ProfileFragment").w("No presigned URL for issue report upload.")
                return false
            }
            val body = tmpFile.readText().toRequestBody("application/json".toMediaType())
            val response = awsService.uploadAsset(presignedUrl, body).execute()
            response.isSuccessful.also {
                Timber.tag("ProfileFragment").d("Issue report upload status=${response.code()} path=$uploadPath")
            }
        } catch (t: Throwable) {
            Timber.tag("ProfileFragment").e(t, "Issue report upload failed")
            false
        } finally {
            tmpFile.delete()
        }
    }

    private fun signOut() {
        val ctx = context?.applicationContext ?: return
        Amplify.Auth.signOut(
            {
                requireActivity().runOnUiThread {
                    Toast.makeText(ctx, "Signed out", Toast.LENGTH_SHORT).show()
                    try {
                        findNavController().navigate(R.id.registerOrLoginFragment)
                    } catch (_: Exception) { /* nav graph may not expose it as action */ }
                }
            },
            { error ->
                requireActivity().runOnUiThread {
                    Toast.makeText(ctx, "Sign out failed: ${error.message}", Toast.LENGTH_LONG).show()
                }
            }
        )
    }
}
