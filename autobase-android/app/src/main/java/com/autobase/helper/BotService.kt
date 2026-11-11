package com.autobase.helper

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.util.Base64
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
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.File
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
            set(value) { field = value; BotLog.add(value) }
        @Volatile var running: Boolean = false
        @Volatile var lastEventPackage: String? = null
        const val MATCH_THRESHOLD = 0.85
        const val MAX_HOURS = 10L        // auto-stop so it never runs forever
        const val STUCK_LIMIT = 6        // same spot tapped this many times in a row => stop
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var overlay: View? = null

    override fun onServiceConnected() {
        instance = this
        BotLog.init(filesDir)
        status = "Ready. Press Start."
        OpenCVLoader.initLocal()
        addKeepAwakeOverlay()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val p = event.packageName?.toString() ?: return
        // ignore our own windows, the status bar/shade and keyboards
        if (p == packageName || p == "com.android.systemui" || p.contains("inputmethod") || p.contains("keyboard")) return
        lastEventPackage = p
    }
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
        job = scope.launch {
            if (Prefs.aiMode(this@BotService)) runAiLoop() else runTemplateLoop()
        }
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

    private suspend fun runTemplateLoop() {
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

    /** Package name of the app currently in front (used by the "detect game package" button). */
    fun currentPackage(): String? = rootInActiveWindow?.packageName?.toString() ?: lastEventPackage

    private val blockedWords = Regex(
        "\\b(buy|purchase|shop|gems?|diamonds?|top-?up|recharge|deals?|vip|speed ?up|pay|price|use item)\\b|[$\u20b9]",
        RegexOption.IGNORE_CASE
    )

    private suspend fun runAiLoop() {
        val gemini = Prefs.provider(this) == "gemini"
        val key = if (gemini) Prefs.geminiKey(this) else Prefs.apiKey(this)
        val model = if (gemini) Prefs.geminiModel(this) else Prefs.model(this)
        val pkg = Prefs.gamePackage(this)
        if (key.isBlank()) { stopBot("AI mode: enter your API key"); return }
        if (pkg.isBlank()) {
            stopBot("AI mode needs the game package so other apps are never sent. Use 'Detect game package'.")
            return
        }
        val agent: Brain = if (gemini) GeminiAgent(key, model) else ClaudeAgent(key, model)
        BotLog.add("AI loop start: provider=${if (gemini) "gemini" else "claude"} model=$model pkg=$pkg interval=${Prefs.intervalSec(this)}s cap=${Prefs.maxCallsPerDay(this)}")
        var rateLimited = 0
        var overloaded = 0
        val startedAt = System.currentTimeMillis()
        val history = ArrayDeque<String>()
        var consecutiveErrors = 0
        var lastTap: Pair<Int, Int>? = null
        var sameSpot = 0

        while (scope.isActive && running) {
            if (System.currentTimeMillis() - startedAt > MAX_HOURS * 3_600_000L) {
                stopBot("Auto-stopped after $MAX_HOURS hours"); return
            }
            if (currentPackage() != pkg) {
                status = "Waiting for the game (now in: ${currentPackage()}, expected: $pkg)"
                delay(5_000)
                continue
            }
            val used = Prefs.callsToday(this)
            val cap = Prefs.maxCallsPerDay(this)
            if (used >= cap) { stopBot("Daily AI call limit reached ($cap)"); return }

            val bmp = screenshot()
            if (bmp == null) {
                status = "Screenshot failed, retrying"
                delay(3_000)
                continue
            }
            val screenW = bmp.width
            val screenH = bmp.height
            val sendW = 720
            val sendH = (bmp.height * sendW.toFloat() / bmp.width).toInt()
            val factor = bmp.width.toFloat() / sendW
            val small = Bitmap.createScaledBitmap(bmp, sendW, sendH, true)
            bmp.recycle()
            val out = ByteArrayOutputStream()
            small.compress(Bitmap.CompressFormat.JPEG, 70, out)
            val b64 = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)

            status = "Asking AI... (call ${used + 1}/$cap)"
            Prefs.addCall(this)
            val result = withContext(Dispatchers.IO) {
                runCatching { agent.decide(b64, sendW, sendH, history.toList()) }
            }
            val decision = result.getOrNull()
            saveDebug(small, decision)   // keeps the last AI view (with its box) so it can be shared
            if (result.exceptionOrNull() is OverloadedException) {
                overloaded++
                if (overloaded >= 20) {
                    stopBot("The AI service stayed overloaded. Stopped. Try again later.")
                    return
                }
                val wait = (30_000L * overloaded).coerceAtMost(300_000L)
                status = "AI service busy (503). Retry $overloaded/20 in ${wait / 1000}s"
                delay(wait)
                continue
            }
            if (result.exceptionOrNull() is RateLimitException) {
                rateLimited++
                if (rateLimited >= 6) {
                    stopBot("Free quota used up. Stopped. Try again later (quota resets daily).")
                    return
                }
                status = "Rate limit hit ($rateLimited/6). Waiting 5 minutes"
                delay(300_000)
                continue
            }
            if (decision == null) {
                consecutiveErrors++
                val msg = result.exceptionOrNull()?.message?.take(120) ?: "unknown error"
                if (consecutiveErrors >= 3) {
                    stopBot("AI errors, stopped. Last: $msg")
                    return
                }
                status = "AI error ($consecutiveErrors/3): $msg"
                delay(15_000)
                continue
            }
            consecutiveErrors = 0
            rateLimited = 0
            overloaded = 0
            BotLog.add("AI decided: ${decision.action} '${decision.label}' box=${decision.box} sent=${sendW}x${sendH} screen=${screenW}x${screenH} why=${decision.reason}")

            when (decision.action) {
                "stop" -> { stopBot("AI asked to stop: ${decision.reason}"); return }
                "tap" -> {
                    val b = decision.box
                    val text = decision.label + " " + decision.reason
                    if (b == null || b[2] <= b[0] || b[3] <= b[1] ||
                        b[0] < 0 || b[1] < 0 || b[2] > sendW || b[3] > sendH
                    ) {
                        status = "AI gave an invalid box, skipping"
                        delay(10_000)
                    } else if (blockedWords.containsMatchIn(text)) {
                        status = "Blocked a risky tap: '${decision.label}'"
                        history.addLast("REFUSED to tap '${decision.label}' (money/risky). Choose something else or wait.")
                        while (history.size > 4) history.removeFirst()
                        delay(10_000)
                    } else {
                        val cx = ((b[0] + b[2]) / 2f * factor).toInt()
                        val cy = ((b[1] + b[3]) / 2f * factor).toInt()
                        val here = Pair(cx, cy)
                        sameSpot = if (lastTap != null && near(lastTap, here)) sameSpot + 1 else 1
                        lastTap = here
                        if (sameSpot >= 4) {
                            stopBot("Stuck: same spot tapped 4 times ('${decision.label}')")
                            return
                        }
                        BotLog.add("TAP at ($cx,$cy) on screen ${screenW}x${screenH}")
                        tap(cx + Random.nextInt(-4, 5), cy + Random.nextInt(-4, 5))
                        status = "AI tapped '${decision.label}': ${decision.reason}"
                        history.addLast("tapped '${decision.label}' - ${decision.reason}")
                        while (history.size > 4) history.removeFirst()
                        delay(Random.nextLong(3_500, 6_000))   // let the game animate before the next look
                    }
                }
                else -> {
                    sameSpot = 0
                    lastTap = null
                    history.addLast("waited - ${decision.reason}")
                    while (history.size > 4) history.removeFirst()
                    val base = Prefs.intervalSec(this) * 1000L
                    status = "AI: nothing to do (${decision.reason}). Next look in ~${base / 1000}s"
                    delay(base + Random.nextLong(0, 5_000))
                }
            }
        }
    }

    /** Draws the AI's box on the image it saw and stores it; always recycles [bmp]. */
    private fun saveDebug(bmp: Bitmap, d: AiDecision?) {
        runCatching {
            val dbg = bmp.copy(Bitmap.Config.ARGB_8888, true)
            val c = Canvas(dbg)
            val box = Paint().apply { color = Color.RED; style = Paint.Style.STROKE; strokeWidth = 5f }
            val b = d?.box
            if (b != null) c.drawRect(b[0].toFloat(), b[1].toFloat(), b[2].toFloat(), b[3].toFloat(), box)
            val txt = Paint().apply { color = Color.YELLOW; textSize = 28f; setShadowLayer(3f, 0f, 0f, Color.BLACK) }
            c.drawText(if (d == null) "AI: no answer" else "${d.action} '${d.label}'", 12f, 36f, txt)
            val dir = File(cacheDir, "debug").also { it.mkdirs() }
            File(dir, "last.jpg").outputStream().use { dbg.compress(Bitmap.CompressFormat.JPEG, 80, it) }
            dbg.recycle()
        }
        bmp.recycle()
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
