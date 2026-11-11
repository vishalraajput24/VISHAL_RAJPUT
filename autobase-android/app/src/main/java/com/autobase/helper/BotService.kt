package com.autobase.helper

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.PixelFormat
import android.view.Display
import android.view.View
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import org.opencv.android.OpenCVLoader
import org.opencv.core.Mat
import kotlin.coroutines.resume
import kotlin.math.abs
import kotlin.random.Random

/**
 * Accessibility service that, while "running":
 *  1. takes a screenshot every few seconds,
 *  2. looks for the saved button images (collect / help / upgrade),
 *  3. taps the first one it finds, with small random jitter and delays.
 */
class BotService : AccessibilityService() {

    companion object {
        @Volatile var instance: BotService? = null
        @Volatile var status: String = "Service not enabled"
        @Volatile var running: Boolean = false
        const val MATCH_THRESHOLD = 0.85
        const val MAX_HOURS = 10L        // auto-stop so it never runs forever
        const val STUCK_LIMIT = 6        // same spot tapped this many times in a row => stop
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var overlay: View? = null

    override fun onServiceConnected() {
        instance = this
        status = "Ready. Press Start."
        OpenCVLoader.initLocal()
        addKeepAwakeOverlay()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    override fun onDestroy() {
        stopBot("Service stopped")
        removeOverlay()
        scope.cancel()
        instance = null
        super.onDestroy()
    }

    fun startBot() {
        if (running) return
        running = true
        job = scope.launch { runLoop() }
    }

    fun stopBot(msg: String = "Stopped") {
        running = false
        job?.cancel()
        job = null
        status = msg
    }

    // 1x1 invisible overlay with keepScreenOn so the screen doesn't turn off overnight
    private fun addKeepAwakeOverlay() {
        if (overlay != null) return
        val wm = getSystemService(WINDOW_SERVICE) as WindowManager
        val v = View(this).apply { keepScreenOn = true }
        val lp = WindowManager.LayoutParams(
            1, 1,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT
        )
        runCatching { wm.addView(v, lp); overlay = v }
    }

    private fun removeOverlay() {
        val v = overlay ?: return
        runCatching { (getSystemService(WINDOW_SERVICE) as WindowManager).removeView(v) }
        overlay = null
    }

    private suspend fun runLoop() {
        val templates = Templates.loadAll(this)
        if (templates.isEmpty()) {
            stopBot("No button images set. Add at least one.")
            return
        }
        val startedAt = System.currentTimeMillis()
        var lastTap: Pair<Int, Int>? = null
        var sameSpot = 0

        try {
            while (scope.isActive && running) {
                if (System.currentTimeMillis() - startedAt > MAX_HOURS * 3_600_000L) {
                    stopBot("Auto-stopped after $MAX_HOURS hours")
                    return
                }

                val pkg = Prefs.gamePackage(this)
                if (pkg.isNotEmpty() && rootInActiveWindow?.packageName?.toString() != pkg) {
                    status = "Waiting for the game to be on screen"
                    delay(5_000)
                    continue
                }

                val bmp = screenshot()
                if (bmp == null) {
                    status = "Screenshot failed, retrying"
                    delay(3_000)
                    continue
                }
                val screen = Templates.prepare(bmp)
                bmp.recycle()

                var tapped: String? = null
                for ((name, tpl) in templates) {
                    val hit = Templates.find(screen, tpl, MATCH_THRESHOLD) ?: continue
                    sameSpot = if (lastTap != null && near(lastTap, hit)) sameSpot + 1 else 1
                    lastTap = hit
                    if (sameSpot >= STUCK_LIMIT) {
                        screen.release()
                        stopBot("Stuck on '$name' (tapped same spot $STUCK_LIMIT times). Stopped.")
                        return
                    }
                    tap(hit.first + Random.nextInt(-4, 5), hit.second + Random.nextInt(-4, 5))
                    tapped = name
                    break
                }
                screen.release()

                if (tapped != null) {
                    status = "Tapped '$tapped'"
                    delay(Random.nextLong(1_500, 3_000))     // quick re-check, there may be more
                } else {
                    sameSpot = 0
                    lastTap = null
                    val base = Prefs.intervalSec(this) * 1000L
                    status = "Nothing to do. Next check in ~${base / 1000}s"
                    delay(base + Random.nextLong(0, 5_000))
                }
            }
        } finally {
            templates.values.forEach(Mat::release)
        }
    }

    private fun near(a: Pair<Int, Int>, b: Pair<Int, Int>) =
        abs(a.first - b.first) < 40 && abs(a.second - b.second) < 40

    private suspend fun screenshot(): Bitmap? = suspendCancellableCoroutine { cont ->
        takeScreenshot(Display.DEFAULT_DISPLAY, mainExecutor, object : TakeScreenshotCallback {
            override fun onSuccess(result: ScreenshotResult) {
                val hw = Bitmap.wrapHardwareBuffer(result.hardwareBuffer, result.colorSpace)
                val sw = hw?.copy(Bitmap.Config.ARGB_8888, false)
                hw?.recycle()
                result.hardwareBuffer.close()
                if (cont.isActive) cont.resume(sw)
            }

            override fun onFailure(errorCode: Int) {
                if (cont.isActive) cont.resume(null)
            }
        })
    }

    private fun tap(x: Int, y: Int) {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val stroke = GestureDescription.StrokeDescription(path, 0, Random.nextLong(40, 90))
        dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }
}
