package com.autobase.helper

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Bitmap
import android.graphics.BitmapFactory
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
        SkillStore.init(this)
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
        "\\b(buy|purchase|shop|gems?|diamonds?|top-?up|recharge|deals?|vip|speed ?up|pay|price|use item|attack|rally|teleport|relocate|migrate|disband|dismiss|delete)\\b|[$\u20b9]",
        RegexOption.IGNORE_CASE
    )

    private var lastFull: Bitmap? = null
    private var rec: Rec? = null

    private class Rec(val task: String, var repeatMin: Int) {
        val steps = mutableListOf<SkillStep>()
        var invalid = false
    }

    private enum class SkillResult { OK, PARTIAL, NOT_APPLICABLE, FAILED }

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
        BotLog.add("AI loop start: provider=${if (gemini) "gemini" else "claude"} model=$model pkg=$pkg interval=${Prefs.intervalSec(this)}s cap=${Prefs.maxCallsPerDay(this)} skills=${SkillStore.summary()}")
        var rateLimited = 0
        var overloaded = 0
        val startedAt = System.currentTimeMillis()
        val history = ArrayDeque<String>()
        var consecutiveErrors = 0
        var lastTap: Pair<Int, Int>? = null
        var sameSpot = 0
        var nextAiAt = 0L

        try {
            while (scope.isActive && running) {
                if (System.currentTimeMillis() - startedAt > MAX_HOURS * 3_600_000L) {
                    stopBot("Auto-stopped after $MAX_HOURS hours"); return
                }
                if (currentPackage() != pkg) {
                    finishRecording()
                    status = "Waiting for the game (now in: ${currentPackage()}, expected: $pkg)"
                    delay(5_000)
                    continue
                }

                // 1) learned routines that are due run without calling the AI
                val due = SkillStore.due(System.currentTimeMillis())
                if (due.isNotEmpty()) {
                    finishRecording()
                    for (sk in due) {
                        if (!running) break
                        val res = runSkill(sk)
                        val t = System.currentTimeMillis()
                        when (res) {
                            SkillResult.OK -> SkillStore.markOk(sk.id, t)
                            SkillResult.PARTIAL -> SkillStore.markPartial(sk.id, t)
                            SkillResult.NOT_APPLICABLE -> SkillStore.markNotApplicable(sk.id, t)
                            SkillResult.FAILED -> { SkillStore.markFail(sk.id, t); nextAiAt = 0L }
                        }
                        BotLog.add("Skill '${sk.id}' finished: $res")
                        if (res != SkillResult.NOT_APPLICABLE) delay(2_000)
                    }
                    continue
                }

                // 2) the AI is only asked when its own wait time is over
                val waitMs = nextAiAt - System.currentTimeMillis()
                if (waitMs > 0) {
                    delay(minOf(3_000L, waitMs).coerceAtLeast(300L))
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
                lastFull?.recycle()
                lastFull = bmp      // full-size copy is kept so a tapped button can be cropped as a template
                val out = ByteArrayOutputStream()
                small.compress(Bitmap.CompressFormat.JPEG, 70, out)
                val b64 = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)

                status = "Asking AI... (call ${used + 1}/$cap)"
                Prefs.addCall(this)
                val result = withContext(Dispatchers.IO) {
                    runCatching { agent.decide(b64, sendW, sendH, history.toList(), SkillStore.summary()) }
                }
                val decision = result.getOrNull()
                saveDebug(small, decision)   // keeps the last AI view (with its box) so it can be shared
                if (result.exceptionOrNull() is OverloadedException) {
                    finishRecording()
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
                    finishRecording()
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
                    finishRecording()
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
                BotLog.add("AI decided: ${decision.action} '${decision.label}' task=${decision.task} repeat=${decision.repeatMin} box=${decision.box} sent=${sendW}x${sendH} screen=${screenW}x${screenH} why=${decision.reason}")
                if (decision.notes.isNotBlank()) SkillStore.addNote(decision.notes)

                when (decision.action) {
                    "stop" -> { finishRecording(); stopBot("AI asked to stop: ${decision.reason}"); return }
                    "tap" -> {
                        val b = decision.box
                        val text = decision.label + " " + decision.reason
                        if (b == null || b[2] <= b[0] || b[3] <= b[1] ||
                            b[0] < 0 || b[1] < 0 || b[2] > sendW || b[3] > sendH
                        ) {
                            finishRecording()
                            status = "AI gave an invalid box, skipping"
                            delay(10_000)
                        } else if (blockedWords.containsMatchIn(text)) {
                            finishRecording()
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
                                finishRecording()
                                stopBot("Stuck: same spot tapped 4 times ('${decision.label}')")
                                return
                            }
                            BotLog.add("TAP at ($cx,$cy) on screen ${screenW}x${screenH}")
                            tap(cx + Random.nextInt(-4, 5), cy + Random.nextInt(-4, 5))
                            recordStep(decision, b, factor)
                            status = "AI tapped '${decision.label}': ${decision.reason}"
                            history.addLast("tapped '${decision.label}' - ${decision.reason}")
                            while (history.size > 4) history.removeFirst()
                            nextAiAt = System.currentTimeMillis() + Random.nextLong(3_500, 6_000)   // let the game animate first
                        }
                    }
                    else -> {
                        finishRecording()
                        sameSpot = 0
                        lastTap = null
                        history.addLast("waited - ${decision.reason}")
                        while (history.size > 4) history.removeFirst()
                        val minWait = Prefs.intervalSec(this)
                        val ask = decision.waitSec
                        val waitSec = if (ask != null) maxOf(minWait, minOf(ask, 300)) else minWait
                        status = "AI: nothing to do (${decision.reason}). Next look in ~${waitSec}s"
                        nextAiAt = System.currentTimeMillis() + waitSec * 1000L + Random.nextLong(0, 5_000)
                    }
                }
            }
        } finally {
            finishRecording()
            lastFull?.recycle()
            lastFull = null
        }
    }

    // ---------------- learning: record what the AI did ----------------

    private fun recordStep(d: AiDecision, b: List<Int>, factor: Float) {
        val full = lastFull ?: return
        val task = d.task.trim().lowercase().replace(Regex("[^a-z0-9_]+"), "_").trim('_')
        if (task.isEmpty()) { finishRecording(); return }
        if (rec?.task != task) { finishRecording(); rec = Rec(task, 0) }
        val r = rec ?: return
        val rep = d.repeatMin
        if (rep != null && rep > 0) r.repeatMin = rep

        val sw = full.width
        val sh = full.height
        var x1 = (b[0] * factor).toInt()
        var y1 = (b[1] * factor).toInt()
        var x2 = (b[2] * factor).toInt()
        var y2 = (b[3] * factor).toInt()
        val padX = ((x2 - x1) * 0.06f).toInt()
        val padY = ((y2 - y1) * 0.06f).toInt()
        x1 -= padX; x2 += padX; y1 -= padY; y2 += padY
        // very small boxes are padded to a minimum size so matching has something to hold on to
        if (x2 - x1 < 64) { val c = (x1 + x2) / 2; x1 = c - 32; x2 = c + 32 }
        if (y2 - y1 < 64) { val c = (y1 + y2) / 2; y1 = c - 32; y2 = c + 32 }
        x1 = x1.coerceIn(0, sw - 1); y1 = y1.coerceIn(0, sh - 1)
        x2 = x2.coerceIn(x1 + 1, sw); y2 = y2.coerceIn(y1 + 1, sh)
        val w = x2 - x1
        val h = y2 - y1
        if (w.toLong() * h > sw.toLong() * sh * 35 / 100 || r.steps.size >= 8) { r.invalid = true; return }

        runCatching {
            val crop = Bitmap.createBitmap(full, x1, y1, w, h)
            val name = "${task}_${r.steps.size}_${System.currentTimeMillis() % 1_000_000}.png"
            SkillStore.imageFile(name).outputStream().use { crop.compress(Bitmap.CompressFormat.PNG, 100, it) }
            crop.recycle()
            r.steps.add(SkillStep(d.label.ifBlank { "step" }, name, 2_500))
        }.onFailure { r.invalid = true }
    }

    /** Called when the AI's routine ends (it waits, changes task, errors out...). Saves it as a skill if it qualifies. */
    private fun finishRecording() {
        val r = rec ?: return
        rec = null
        fun discard() = r.steps.forEach { SkillStore.imageFile(it.img).delete() }
        if (r.invalid || r.steps.isEmpty() || r.repeatMin <= 0) { discard(); return }
        val old = SkillStore.get(r.task)
        if (old != null && old.enabled && old.fails == 0 && old.partials < 2) { discard(); return }   // keep the proven version
        SkillStore.upsert(
            Skill(
                id = r.task, intervalMin = r.repeatMin, lastRun = System.currentTimeMillis(), nextTry = 0L,
                ok = 0, partials = 0, fails = 0, enabled = true, steps = r.steps.toList()
            )
        )
        BotLog.add("LEARNED skill '${r.task}': ${r.steps.joinToString(" > ") { it.label }} (every ${r.repeatMin} min)")
    }

    // ---------------- replay: run a learned skill without the AI ----------------

    private val closerLabel = Regex("^(x|\u00d7|close.*|back)$", RegexOption.IGNORE_CASE)

    private suspend fun runSkill(s: Skill): SkillResult {
        val mats: List<Mat?> = s.steps.map { st ->
            val bmp = BitmapFactory.decodeFile(SkillStore.imageFile(st.img).absolutePath)
            if (bmp == null) null else Templates.prepare(bmp).also { bmp.recycle() }
        }
        try {
            status = "Skill '${s.id}' running (no AI)"
            for ((idx, st) in s.steps.withIndex()) {
                val tpl = mats[idx] ?: return SkillResult.FAILED
                val hit = waitForMatch(tpl, if (idx == 0) 0L else 9_000L)
                if (hit == null) {
                    if (idx == 0) return SkillResult.NOT_APPLICABLE
                    // Something expected is missing (often "nothing to do right now"): just close what is open.
                    for (j in idx until s.steps.size) {
                        if (!closerLabel.matches(s.steps[j].label.trim())) continue
                        val m = mats[j] ?: continue
                        val h2 = waitForMatch(m, 3_000L) ?: continue
                        tap(h2.first + Random.nextInt(-4, 5), h2.second + Random.nextInt(-4, 5))
                        delay(1_500)
                    }
                    return SkillResult.PARTIAL
                }
                tap(hit.first + Random.nextInt(-4, 5), hit.second + Random.nextInt(-4, 5))
                delay(st.afterMs + Random.nextLong(0, 800))
            }
            return SkillResult.OK
        } finally {
            mats.forEach { it?.release() }
        }
    }

    /** Looks for [tpl] on screen, retrying until [timeoutMs] passes. Returns the centre in screen pixels or null. */
    private suspend fun waitForMatch(tpl: Mat, timeoutMs: Long): Pair<Int, Int>? {
        val end = System.currentTimeMillis() + timeoutMs
        while (true) {
            val bmp = screenshot()
            if (bmp != null) {
                val screen = Templates.prepare(bmp)
                bmp.recycle()
                val hit = Templates.find(screen, tpl, 0.78)
                screen.release()
                if (hit != null) return hit
            }
            if (System.currentTimeMillis() >= end || !running) return null
            delay(1_300)
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
