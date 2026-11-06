package com.screenlake.recorder.authentication

import android.content.Context
import android.content.SharedPreferences
import android.widget.Toast
import androidx.lifecycle.MutableLiveData
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys
import com.amazonaws.mobile.auth.core.internal.util.ThreadUtils.runOnUiThread
import com.amazonaws.mobile.client.AWSMobileClient
import com.amazonaws.mobile.client.Callback
import com.amazonaws.mobile.client.UserStateDetails
import com.amazonaws.mobile.client.results.SignInResult
import com.amplifyframework.auth.AuthException
import com.amplifyframework.auth.AuthUserAttributeKey
import com.amplifyframework.auth.cognito.AWSCognitoAuthSession
import com.amplifyframework.auth.cognito.options.AWSCognitoAuthSignUpOptions
import com.amplifyframework.auth.options.AuthSignUpOptions
import com.amplifyframework.core.Amplify
import com.screenlake.BuildConfig
import com.screenlake.data.model.EmailPasswordData
import com.screenlake.data.repository.AmplifyRepository
import com.screenlake.recorder.services.util.CognitoErrorHelper
import com.screenlake.ui.fragments.onboarding.RegisterConfirmPassword
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import timber.log.Timber
import java.lang.ref.WeakReference
import java.security.MessageDigest
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class CloudAuthentication @Inject constructor(
    private val amplifyRepository: AmplifyRepository
) {

    /**
     * Fetches the current authentication session.
     */
    fun fetchCurrentAuthSession() {
        amplifyRepository.fetchAuthSession(
            onSuccess = { authSession ->
                val session = authSession as AWSCognitoAuthSession
                if (session.isSignedIn) {
                    Timber.d("User is signed in")
                } else if (session.awsCredentials.error is AuthException.SignedOutException || session.userSub.error is AuthException.SignedOutException) {
                    Timber.d("User is signed out")
                } else if (session.awsCredentials.error == null) {
                    Timber.d("Session is valid")
                }
            },
            onError = { error ->
                Timber.e("Fetch auth session error: $error")
            }
        )
    }

    /**
     * Automatically signs in the user.
     *
     * @param context The context.
     */
    fun autoSignIn(context: WeakReference<Context>) {
        val creds = getEncryptedCredentials(context)
        signInAsync(creds.email, creds.password)
    }

    /**
     * Clears the user authentication data.
     */
    fun clearUserAuth() {
        // Implement the logic to clear user authentication data
    }

    /**
     * Signs out the user.
     *
     * @param isLoggedOut The live data to update the logged-out status.
     */
    fun signOut(isLoggedOut: MutableLiveData<Boolean>) {
        Amplify.Auth.signOut(
            { isLoggedOut.postValue(true) },
            { error -> Timber.e("Sign out error: $error") }
        )
    }

    /**
     * Signs up the user with the provided password.
     *
     * @param password The password.
     */
    fun signUp(password: String, context: Context) {
        // Pass the participant's 4-digit code to the PreSignUp Lambda trigger via
        // clientMetadata. The Lambda rejects the signup if the code does not match
        // the researcher's pre-reservation for this email.
        val options = AWSCognitoAuthSignUpOptions.builder()
            .userAttribute(AuthUserAttributeKey.email(), amplifyRepository.email)
            .clientMetadata(mapOf("panel_code" to amplifyRepository.code))
            .build()

        Amplify.Auth.signUp(amplifyRepository.email, password, options,
            { result ->
                Timber.d("Sign up result: $result")
                RegisterConfirmPassword.isRegisteredIn.postValue(true)
             },
            { error ->
                RegisterConfirmPassword.isRegisteredIn.postValue(false)
                // Surface the Lambda rejection message if present. Cognito wraps
                // PreSignUp exceptions in the error cause; CognitoErrorHelper
                // extracts the readable text.
                val readable = CognitoErrorHelper.getReadableMessage(error)
                RegisterConfirmPassword.loginErrorMsg.postValue(readable ?: error.message)
                runOnUiThread {
                    Toast.makeText(context, readable, Toast.LENGTH_LONG).show()
                }
                Timber.e("Sign up error: $error")
            }
        )
    }

    /**
     * Retrieves the encrypted shared preferences.
     *
     * @param context The context.
     * @return The encrypted shared preferences.
     */
    private fun getEncryptedSharedPreference(context: WeakReference<Context>): SharedPreferences? {
        val keyGenParameterSpec = MasterKeys.AES256_GCM_SPEC
        val masterKeyAlias = MasterKeys.getOrCreate(keyGenParameterSpec)

        return context.get()?.let {
            // Must match the filename used by RegisterLoadingFragment.createAndEncryptPassword.
            // Previously this read from "encrypted_prefs" while signup wrote to
            // "shared_preferences_filename", so autoSignIn always got empty creds
            // and the Amplify session was never actually established —
            // fetchUserAttributes then returned null, which the record screen
            // surfaced as "could not fetch participant ID".
            EncryptedSharedPreferences.create(
                "shared_preferences_filename",
                masterKeyAlias,
                it,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
            )
        }
    }

    /**
     * Retrieves the encrypted credentials.
     *
     * @param context The context.
     * @return The email and password data.
     */
    private fun getEncryptedCredentials(context: WeakReference<Context>): EmailPasswordData {
        val sharedPreferences = getEncryptedSharedPreference(context)
        return if (sharedPreferences != null) {
            val email = sharedPreferences.getString("email", "") ?: ""
            val password = sharedPreferences.getString("password", "") ?: ""
            EmailPasswordData(email, password)
        } else {
            EmailPasswordData("", "")
        }
    }

    companion object {
        /**
         * Signs in the user with the provided email and password.
         *
         * @param email The email.
         * @param password The password.
         */
        fun signIn(email: String, password: String) {
            Amplify.Auth.signIn(email, password,
                { result ->
                    Timber.d("Sign in result: $result")
                    bridgeAwsMobileClient()
                },
                { error -> Timber.e("Sign in error: $error") }
            )
        }

        /**
         * Signs in the user asynchronously with the provided email and password.
         *
         * @param email The email.
         * @param password The password.
         */
        fun signInAsync(email: String, password: String) {
            Amplify.Auth.signIn(email, password,
                { result ->
                    Timber.d("Sign in result: $result")
                    bridgeAwsMobileClient()
                },
                { error -> Timber.e("Sign in error: $error") }
            )
        }

        /**
         * Tracks whether `Util.generates3ShareUrl` has already initialized the
         * AWSMobileClient singleton. The upload code calls `initialize()` lazily
         * the first time it needs an S3 client; `federatedSignIn()` requires
         * that initialization to have run first, otherwise it throws. We cannot
         * safely call `initialize()` from here because that path depends on
         * `buildAWSConfiguration()` which lives in `Util.kt`, so instead we
         * trigger the Util initialization explicitly before federating.
         */
        @Volatile private var mobileClientBridged = false

        /**
         * Bridges Amplify's Cognito User Pool session to AWSMobileClient so that
         * the legacy S3 upload path (via `Util.generates3ShareUrl`) can obtain
         * authenticated credentials from the Cognito Identity Pool.
         *
         * Without this bridge, AWSMobileClient initializes as anonymous and the
         * Identity Pool (which has `AllowUnauthenticatedIdentities: false`)
         * refuses to issue credentials, so `generatePresignedUrl` throws
         * silently and no file ever reaches S3.
         *
         * This is a best-effort fire-and-forget call. If the Amplify session has
         * no idToken yet, we skip — the next sign-in or app launch will retry.
         */
        fun bridgeAwsMobileClient(context: Context? = null) {
            Amplify.Auth.fetchAuthSession(
                { session ->
                    try {
                        val cognitoSession = session as? AWSCognitoAuthSession ?: return@fetchAuthSession
                        val idToken = cognitoSession.userPoolTokens.value?.idToken
                        if (idToken.isNullOrBlank()) {
                            Timber.w("No idToken available yet; AWSMobileClient bridge skipped.")
                            return@fetchAuthSession
                        }

                        // Ensure AWSMobileClient has been initialized at least
                        // once. `Util().generates3ShareUrl` does this lazily; if
                        // bridging runs first, the subsequent federatedSignIn
                        // would fail with "AWSMobileClient not initialized".
                        if (context != null) {
                            try {
                                com.screenlake.recorder.upload.Util().let { util ->
                                    // Reflection-free way to trigger init without
                                    // actually uploading anything: generate a URL
                                    // for a dummy path. Failures are swallowed.
                                    util.generates3ShareUrl(context, "/dev/null", "warmup/${System.currentTimeMillis()}")
                                }
                            } catch (ignored: Throwable) {
                                // Swallow — we only cared about the init side-effect.
                            }
                        }

                        val provider = "cognito-idp.${BuildConfig.AMAZON_REGION_NAME}.amazonaws.com/${BuildConfig.COGNITO_POOL_ID}"
                        AWSMobileClient.getInstance().federatedSignIn(
                            provider,
                            idToken,
                            object : Callback<UserStateDetails?> {
                                override fun onResult(result: UserStateDetails?) {
                                    mobileClientBridged = true
                                    Timber.d("AWSMobileClient federated sign-in success: ${result?.userState}")
                                }

                                override fun onError(e: Exception?) {
                                    Timber.e(e, "AWSMobileClient federated sign-in failed")
                                }
                            }
                        )
                    } catch (t: Throwable) {
                        Timber.e(t, "bridgeAwsMobileClient failed")
                    }
                },
                { error -> Timber.e("bridgeAwsMobileClient fetchAuthSession error: $error") }
            )
        }
    }
}