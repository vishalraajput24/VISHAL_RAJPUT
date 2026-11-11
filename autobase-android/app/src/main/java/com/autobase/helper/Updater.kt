package com.autobase.helper

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import org.json.JSONArray
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** Finds the newest GitHub release built by CI (tag nightbuilder-build-N), downloads its APK and starts the installer. */
object Updater {
    private const val REPO = "vishalraajput24/VISHAL_RAJPUT"
    private const val TAG_PREFIX = "nightbuilder-build-"

    data class Release(val build: Int, val apkUrl: String)

    /** Returns the release with the highest build number, or null if there is none. Blocking: call off the main thread. */
    fun latest(): Release? {
        val conn = URL("https://api.github.com/repos/$REPO/releases?per_page=20").openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 20_000
            conn.readTimeout = 30_000
            conn.setRequestProperty("Accept", "application/vnd.github+json")
            if (conn.responseCode != 200) throw Exception("GitHub answered HTTP ${conn.responseCode}")
            val arr = JSONArray(conn.inputStream.bufferedReader().readText())
            var best: Release? = null
            for (i in 0 until arr.length()) {
                val r = arr.getJSONObject(i)
                val n = r.optString("tag_name").removePrefix(TAG_PREFIX).toIntOrNull() ?: continue
                if (!r.optString("tag_name").startsWith(TAG_PREFIX)) continue
                val assets = r.optJSONArray("assets") ?: continue
                var url: String? = null
                for (j in 0 until assets.length()) {
                    val a = assets.getJSONObject(j)
                    if (a.optString("name").endsWith(".apk")) url = a.optString("browser_download_url")
                }
                if (url != null && (best == null || n > best.build)) best = Release(n, url)
            }
            return best
        } finally {
            conn.disconnect()
        }
    }

    /** Downloads the APK into the app cache and returns the file. Blocking. */
    fun download(ctx: Context, rel: Release, onProgress: (Int) -> Unit): File {
        val dir = File(ctx.cacheDir, "updates").also { it.mkdirs() }
        dir.listFiles()?.forEach { it.delete() }
        val out = File(dir, "NightBuilder-${rel.build}.apk")
        val conn = URL(rel.apkUrl).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 20_000
            conn.readTimeout = 60_000
            conn.instanceFollowRedirects = true
            if (conn.responseCode != 200) throw Exception("Download failed: HTTP ${conn.responseCode}")
            val total = conn.contentLengthLong
            var done = 0L
            var lastPct = -1
            conn.inputStream.use { input ->
                out.outputStream().use { output ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        done += n
                        if (total > 0) {
                            val pct = (done * 100 / total).toInt()
                            if (pct != lastPct) { lastPct = pct; onProgress(pct) }
                        }
                    }
                }
            }
            if (out.length() < 100_000) throw Exception("Downloaded file looks too small")
            return out
        } finally {
            conn.disconnect()
        }
    }

    /** Opens the system installer for the downloaded APK (the user confirms with one tap). */
    fun install(ctx: Context, apk: File) {
        val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", apk)
        val i = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(i)
    }
}
