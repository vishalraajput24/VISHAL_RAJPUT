package com.autobase.helper

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Google Gemini (AI Studio key) over plain HTTPS. Boxes come back as [ymin, xmin, ymax, xmax] on a 0..1000 scale. */
class GeminiAgent(private val apiKey: String, private val model: String) : Brain {

    private fun schema(): JSONObject {
        fun str(desc: String) = JSONObject().put("type", "STRING").put("description", desc)
        return JSONObject()
            .put("type", "OBJECT")
            .put(
                "properties", JSONObject()
                    .put("action", JSONObject().put("type", "STRING").put("enum", JSONArray(listOf("tap", "wait", "stop"))))
                    .put("label", str("visible text or short name of the button to tap"))
                    .put("reason", str("one short sentence: why this action"))
                    .put("wait_seconds", JSONObject().put("type", "INTEGER").put("description", "only for wait: seconds to sleep before looking again (10-300)"))
                    .put(
                        "box_2d", JSONObject().put("type", "ARRAY")
                            .put("description", "[ymin, xmin, ymax, xmax] of the button, normalized to 0-1000. Only for tap.")
                            .put("items", JSONObject().put("type", "INTEGER"))
                    )
            )
            .put("required", JSONArray(listOf("action", "reason")))
    }

    /** The user's model first, then lighter/alternative aliases. Each model has its own quota and load. */
    /** Ask Gemini not to spend time 'thinking' (faster, cheaper). Turned off automatically if the model rejects it. */
    private var thinkingOff = true

    private val candidates = listOf(model, "gemini-flash-lite-latest", "gemini-flash-latest").distinct()

    override fun decide(jpegBase64: String, width: Int, height: Int, history: List<String>): AiDecision {
        val body = buildBody(jpegBase64, history)
        var rate: RateLimitException? = null
        var busy: OverloadedException? = null
        for ((i, m) in candidates.withIndex()) {
            try {
                return call(m, body, width, height)
            } catch (e: RateLimitException) {
                rate = e
            } catch (e: OverloadedException) {
                busy = e
            } catch (e: Exception) {
                if (thinkingOff && e.message?.contains("think", ignoreCase = true) == true) {
                    thinkingOff = false
                    return decide(jpegBase64, width, height, history)   // retry once without the override
                }
                if (i == 0) throw e   // bad key / bad request on the user's own model: report it
            }
        }
        throw busy ?: rate ?: Exception("No Gemini model answered")
    }

    private fun buildBody(jpegBase64: String, history: List<String>): JSONObject {
        val prompt = "Your last actions (oldest first):\n${AiPrompts.history(history)}\n\n" +
            "Decide the next action. For tap, box_2d is [ymin, xmin, ymax, xmax] normalized to 0-1000."

        return JSONObject()
            .put("systemInstruction", JSONObject().put("parts", JSONArray().put(JSONObject().put("text", AiPrompts.SYSTEM))))
            .put(
                "contents", JSONArray().put(
                    JSONObject().put("role", "user").put(
                        "parts", JSONArray()
                            .put(JSONObject().put("inline_data", JSONObject().put("mime_type", "image/jpeg").put("data", jpegBase64)))
                            .put(JSONObject().put("text", prompt))
                    )
                )
            )
            .put(
                "generationConfig", JSONObject()
                    .put("responseMimeType", "application/json")
                    .put("responseSchema", schema())
                    .apply { if (thinkingOff) put("thinkingConfig", JSONObject().put("thinkingBudget", 0)) }
            )

    }

    private fun call(model: String, body: JSONObject, width: Int, height: Int): AiDecision {
        val conn = URL("https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent")
            .openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 20_000
            conn.readTimeout = 90_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("x-goog-api-key", apiKey)
            conn.outputStream.use { it.write(body.toString().toByteArray()) }

            val code = conn.responseCode
            if (code == 429) throw RateLimitException("HTTP 429: free quota / rate limit reached")
            if (code == 500 || code == 502 || code == 503 || code == 504) throw OverloadedException("HTTP $code: Google is busy")
            if (code != 200) {
                val err = conn.errorStream?.bufferedReader()?.readText().orEmpty()
                val msg = runCatching { JSONObject(err).getJSONObject("error").getString("message") }.getOrDefault(err)
                throw Exception("HTTP $code: ${msg.take(160)}")
            }
            val text = conn.inputStream.bufferedReader().readText()
            val part = JSONObject(text).optJSONArray("candidates")
                ?.optJSONObject(0)?.optJSONObject("content")?.optJSONArray("parts")?.optJSONObject(0)
                ?: return AiDecision("wait", null, "", "empty or blocked response")
            val out = JSONObject(part.optString("text", "{}"))

            var box: List<Int>? = null
            out.optJSONArray("box_2d")?.let { a ->
                if (a.length() == 4) {
                    val ymin = a.getInt(0); val xmin = a.getInt(1); val ymax = a.getInt(2); val xmax = a.getInt(3)
                    box = listOf(xmin * width / 1000, ymin * height / 1000, xmax * width / 1000, ymax * height / 1000)
                }
            }
            val ws = out.optInt("wait_seconds", -1).takeIf { it > 0 }
            return AiDecision(out.optString("action", "wait").lowercase(), box, out.optString("label"), out.optString("reason"), ws)
        } catch (e: java.io.IOException) {
            throw OverloadedException("network/timeout: ${e.message}")   // includes read timeouts: retry later
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        /** Model ids that support generateContent and look like Flash models (cheap/free-tier friendly). */
        fun listFlashModels(apiKey: String): List<String> {
            val conn = URL("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200")
                .openConnection() as HttpURLConnection
            try {
                conn.connectTimeout = 20_000
                conn.readTimeout = 30_000
                conn.setRequestProperty("x-goog-api-key", apiKey)
                if (conn.responseCode != 200) {
                    throw Exception("HTTP ${conn.responseCode}: ${conn.errorStream?.bufferedReader()?.readText()?.take(120)}")
                }
                val arr = JSONObject(conn.inputStream.bufferedReader().readText()).getJSONArray("models")
                val out = ArrayList<String>()
                for (i in 0 until arr.length()) {
                    val m = arr.getJSONObject(i)
                    val name = m.getString("name").removePrefix("models/")
                    val methods = m.optJSONArray("supportedGenerationMethods")?.toString().orEmpty()
                    if (name.contains("flash") && methods.contains("generateContent")) out.add(name)
                }
                return out
            } finally {
                conn.disconnect()
            }
        }
    }
}
