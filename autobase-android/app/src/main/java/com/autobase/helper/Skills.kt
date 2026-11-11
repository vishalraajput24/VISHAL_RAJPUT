package com.autobase.helper

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

data class SkillStep(val label: String, val img: String, val afterMs: Long)

/** A routine the AI performed successfully once; the app can replay it later without calling the AI. */
data class Skill(
    val id: String,
    var intervalMin: Int,
    var lastRun: Long,
    var nextTry: Long,
    var ok: Int,
    var partials: Int,
    var fails: Int,
    var enabled: Boolean,
    val steps: List<SkillStep>
)

/** Learned routines (skills.json + one cropped PNG per step) and short notes about things the AI noticed. */
object SkillStore {
    private val skills = mutableListOf<Skill>()
    private val notes = mutableListOf<String>()
    private var dir: File? = null

    @Synchronized
    fun init(ctx: Context) {
        if (dir != null) return
        val d = File(ctx.filesDir, "skills").also { it.mkdirs() }
        dir = d
        runCatching {
            val f = File(d, "skills.json")
            if (f.exists()) {
                val arr = JSONArray(f.readText())
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    val st = o.getJSONArray("steps")
                    val steps = (0 until st.length()).map {
                        val s = st.getJSONObject(it)
                        SkillStep(s.getString("label"), s.getString("img"), s.optLong("afterMs", 2500))
                    }
                    skills.add(
                        Skill(
                            id = o.getString("id"), intervalMin = o.getInt("intervalMin"),
                            lastRun = o.optLong("lastRun"), nextTry = o.optLong("nextTry"),
                            ok = o.optInt("ok"), partials = o.optInt("partials"), fails = o.optInt("fails"),
                            enabled = o.optBoolean("enabled", true), steps = steps
                        )
                    )
                }
            }
            val nf = File(d, "notes.txt")
            if (nf.exists()) notes.addAll(nf.readLines().filter { it.isNotBlank() })
        }
    }

    fun imageFile(name: String): File = File(dir ?: File("."), name)

    @Synchronized
    private fun save() {
        val d = dir ?: return
        val arr = JSONArray()
        for (s in skills) {
            val st = JSONArray()
            s.steps.forEach { st.put(JSONObject().put("label", it.label).put("img", it.img).put("afterMs", it.afterMs)) }
            arr.put(
                JSONObject().put("id", s.id).put("intervalMin", s.intervalMin).put("lastRun", s.lastRun)
                    .put("nextTry", s.nextTry).put("ok", s.ok).put("partials", s.partials).put("fails", s.fails)
                    .put("enabled", s.enabled).put("steps", st)
            )
        }
        runCatching { File(d, "skills.json").writeText(arr.toString()) }
    }

    @Synchronized fun all(): List<Skill> = skills.map { it.copy() }
    @Synchronized fun get(id: String): Skill? = skills.firstOrNull { it.id == id }?.copy()

    @Synchronized
    fun due(now: Long): List<Skill> = skills.filter {
        it.enabled && it.intervalMin > 0 && now >= maxOf(it.lastRun + it.intervalMin * 60_000L, it.nextTry)
    }.map { it.copy() }

    @Synchronized
    fun upsert(s: Skill) {
        val old = skills.indexOfFirst { it.id == s.id }
        if (old >= 0) {
            skills[old].steps.forEach { imageFile(it.img).delete() }
            skills[old] = s
        } else skills.add(s)
        save()
    }

    @Synchronized
    private fun mutate(id: String, f: (Skill) -> Unit) {
        skills.firstOrNull { it.id == id }?.let { f(it); save() }
    }

    fun markOk(id: String, now: Long) = mutate(id) { it.ok++; it.partials = 0; it.fails = 0; it.lastRun = now }
    fun markPartial(id: String, now: Long) = mutate(id) { it.partials++; it.lastRun = now }
    fun markNotApplicable(id: String, now: Long) = mutate(id) { it.nextTry = now + 3 * 60_000L }
    fun markFail(id: String, now: Long) = mutate(id) {
        it.fails++
        it.lastRun = now
        if (it.fails >= 3) it.enabled = false   // the AI will re-learn it the next time it does this routine
    }

    @Synchronized
    fun clear() {
        skills.forEach { s -> s.steps.forEach { imageFile(it.img).delete() } }
        skills.clear()
        save()
    }

    @Synchronized
    fun summary(): String {
        val active = skills.filter { it.enabled }
        return if (active.isEmpty()) "none yet" else active.joinToString(", ") { "${it.id} (every ${it.intervalMin} min)" }
    }

    /** Remembers a short observation ("Cavalry training is idle") once. */
    @Synchronized
    fun addNote(text: String) {
        val t = text.trim().take(160)
        if (t.isEmpty() || notes.any { it.equals(t, ignoreCase = true) }) return
        notes.add(t)
        while (notes.size > 100) notes.removeAt(0)
        runCatching { dir?.let { File(it, "notes.txt").writeText(notes.joinToString("\n")) } }
        BotLog.add("NOTE: $t")
    }

    @Synchronized fun notes(): List<String> = notes.toList()
}
