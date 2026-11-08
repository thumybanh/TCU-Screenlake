package com.screenlake.recorder.services

import android.content.Context
import androidx.hilt.work.HiltWorker
import androidx.lifecycle.MutableLiveData
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.screenlake.data.database.dao.UserDao
import com.screenlake.data.database.entity.ScreenshotZipEntity
import com.screenlake.data.database.entity.UserEntity
import com.screenlake.data.repository.GeneralOperationsRepository
import com.screenlake.recorder.viewmodels.WorkerProgressManager
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.withContext
import timber.log.Timber
import java.io.File

/**
 * Worker for handling the upload of screenshot zip files to the server.
 *
 * This worker fetches the list of screenshot zips to upload, checks for network connectivity,
 * and uploads the files to a remote server. It also handles local log files and records
 * the progress and results of the upload process.
 *
 * @property generalOperationsRepository Instance of GenOp to interact with the database.
 * @property user The current user information.
 * @property zipsToUpload List of screenshot zip files to upload.
 */
@HiltWorker
class UploadWorker @AssistedInject constructor(
    @Assisted context: Context,
    @Assisted workerParams: WorkerParameters,
    private val uploadHandler: UploadHandler,
    private val generalOperationsRepository: GeneralOperationsRepository,
    private val userDao: UserDao
) : CoroutineWorker(context, workerParams) {

    private var user: UserEntity? = null
    private var zipsToUpload: List<ScreenshotZipEntity>? = mutableListOf()

    companion object {
        private const val TAG = "UploadWorker"
        val uploadFeedback = MutableLiveData<String>()
        val mutex = Mutex()
        var isRunning = false
    }

    /**
     * Performs the upload work.
     *
     * This method is called when the worker is executed. It logs the start of the upload service,
     * attempts to upload zip files asynchronously, and logs the completion status.
     *
     * @return Result indicating the success or failure of the upload process.
     */
    override suspend fun doWork(): Result {
        Timber.tag(TAG).d("Upload Worker has started.")

        if (!mutex.tryLock()) {
            Timber.tag(TAG).w("Upload already in progress. Skipping this worker.")
            return Result.failure()
        }

        return try {
            if (isRunning) {
                generalOperationsRepository.saveLog("uploadZipFilesAsync","uploadZipFilesAsync already running.")
                Timber.tag(TAG).w("uploadZipFilesAsync already running.")
                Result.failure()
            } else {
                isRunning = true

                if (!uploadHandler.isNetworkConnected()) {
                    Timber.tag(TAG).d("No network connection. Upload Worker has finished.")
                    return Result.failure()
                }

                // Refresh AWS credentials by bridging the current Amplify session
                // to AWSMobileClient. The Cognito User Pool idToken used by the
                // Identity Pool expires after ~1 hour, so we re-federate on every
                // worker run. Fire-and-forget: the subsequent S3 call will fail
                // and get retried if bridging hasn't completed in time.
                com.screenlake.recorder.authentication.CloudAuthentication
                    .bridgeAwsMobileClient(applicationContext)

                // Sync the TCU code from Cognito's `custom:tcu_code` attribute
                // into the local user record. This is the only authoritative
                // source of the participant's identifier; without it, uploads
                // are refused by `RealUploadHandler.uploadFile`.
                syncTcuCodeFromCognito()

                generalOperationsRepository.saveLog("UPLOAD_SERVICE_RUN", "")

                uploadZipFilesAsync()

                Timber.tag(TAG).d("Upload Worker has finished.")
                WorkerProgressManager.updateProgress("Upload has finished.")
                Result.success()
            }
        } catch (ex: Exception) {
            generalOperationsRepository.saveLog("UPLOAD_SERVICE_RUN_FAIL", ex.stackTraceToString())
            Timber.tag(TAG).e(ex, "Upload Worker failed.")
            Result.failure()
        } finally {
            isRunning = false
            mutex.unlock()
        }
    }

    /**
     * Asynchronously uploads zip files to the server.
     *
     * This method fetches zip files from the database, checks network connectivity,
     * and uploads each file. It handles upload progress updates and cleans up files
     * after successful uploads.
     */
    private suspend fun uploadZipFilesAsync() = withContext(Dispatchers.IO) {
        // Fetch the list of zips to upload and the user information
        zipsToUpload = generalOperationsRepository.getZipsToUpload().take(10)?.toMutableList()
        user = generalOperationsRepository.getUser()

        Timber.tag(TAG).d("Found ${zipsToUpload?.count()} zips to upload.")

        // Add local log files to the list of zips to upload
        addLocalLogFiles()

        var count = 0.0
        zipsToUpload?.let {
            for (zip in it) {
                WorkerProgressManager.updateProgress("Uploading $count of ${zipsToUpload?.count()}")
                zip.file?.let { filePath ->
                    val file = File(filePath)
                    if (file.exists() && uploadHandler.isNetworkConnected()) {
                        Timber.tag(TAG).d("Uploading file ${file.name}")

                        // Upload the file
                        async {
                            uploadHandler.uploadFile(file, zip.id, user)
                        }.await()

                        ScreenshotService.manualUploadPercentComplete.postValue(count / it.count())
                        count++
                    } else {
                        // Handle the case where the file does not exist
                        handleFileNotFound(zip, file)
                        count++
                    }
                } ?: count++
            }
            System.gc() // Suggest garbage collection after uploads
        }
    }

    /**
     * Handles the case where a file to be uploaded is not found.
     *
     * This method logs a warning if a file does not exist on disk and deletes
     * the associated database entry.
     *
     * @param zip The ScreenshotZip object representing the file to upload.
     * @param file The File object for the file to upload.
     */
    private suspend fun handleFileNotFound(zip: ScreenshotZipEntity, file: File) {
        if (!file.exists()) {
            Timber.tag(TAG).w("Could not upload file ${file.name} because it does not exist on disk.")
            zip.id?.let { generalOperationsRepository.deleteScreenshotZip(zip) }
        }
    }

    /**
     * Reads the current Cognito user's `custom:tcu_code` attribute and writes
     * it to the local user record's `emailHash` field. This is the only path
     * by which the TCU code gets persisted locally — the typed signup code is
     * never used. Safe to call on every worker run; no-op if the local value
     * already matches.
     */
    private suspend fun syncTcuCodeFromCognito() {
        try {
            val attrs = com.amplifyframework.kotlin.core.Amplify.Auth.fetchUserAttributes()
            val code = attrs.firstOrNull { it.key.keyString == "custom:tcu_code" }?.value
            val role = attrs.firstOrNull { it.key.keyString == "custom:tcu_role" }?.value

            // Accept LF4342 (participant), RS0001 (researcher), or legacy 1234 (4-digit).
            // The old \d{4} pattern silently rejected LF/RS codes, leaving emailHash
            // stale and all uploads going to the wrong S3 path after an ID migration.
            val validCode = code != null && (
                code.matches(Regex("^LF[1-9]\\d{3}$"))
                || code.matches(Regex("^RS\\d{4}$"))
                || (code.length == 4 && code.all { it.isDigit() })
            )
            if (code.isNullOrBlank() || !validCode) {
                Timber.tag(TAG).w("No valid custom:tcu_code attribute on Cognito user; sync skipped.")
                return
            }

            com.screenlake.data.UserRolePrefs.setRole(applicationContext, role)

            val user = userDao.getUser() ?: run {
                Timber.tag(TAG).w("No local user record; cannot persist TCU code.")
                return
            }
            if (user.emailHash != code) {
                Timber.tag(TAG).d("Syncing TCU code from Cognito: ${user.emailHash} -> $code")
                user.emailHash = code
                userDao.insertUserObj(user)
            }
        } catch (t: Throwable) {
            Timber.tag(TAG).w(t, "syncTcuCodeFromCognito failed; proceeding with existing local value.")
        }
    }

    /**
     * Adds local log files to the list of zips to upload.
     *
     * This method searches for log files in the app's file directory and adds them
     * to the list of screenshot zips to be uploaded.
     */
    private fun addLocalLogFiles() {
        val dir = File(applicationContext.filesDir?.path)

        // Filter and map log files to ScreenshotZip objects
        val csvs = dir.listFiles()?.filter { it.path.contains("log_data") }?.map { ScreenshotZipEntity(file = it.path) }
        csvs?.let {
            (zipsToUpload as? MutableList<ScreenshotZipEntity>)?.addAll(it)
        }
    }
}
