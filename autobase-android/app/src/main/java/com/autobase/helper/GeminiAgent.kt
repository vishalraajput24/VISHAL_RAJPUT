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
                    .put(
                        "box_2d", JSONObject().put("type", "ARRAY")
                            .put("description", "[ymin, xmin, ymax, xmax] of the button, normalized to 0-1000. Only for tap.")
                            .put("items", JSONObject().put("type", "INTEGER"))
                    )
            )
            .put("required", JSONArray(listOf("action", "reason")))
    }

    override fun decide(jpegBase64: String, width: Int, height: Int, history: List<String>): AiDecision {
        val prompt = "Your last actions (oldest first):\n${AiPrompts.history(history)}\n\n" +
            "Decide the next action. For tap, box_2d is [ymin, xmin, ymax, xmax] normalized to 0-1000."

        val body = JSONObject()
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
            )

        val conn = URL("https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent")
            .openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 20_000
            conn.readTimeout = 60_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("x-goog-api-key", apiKey)
            conn.outputStream.use { it.write(body.toString().toByteArray()) }

            val code = conn.responseCode
            if (code == 429) throw RateLimitException("HTTP 429: free quota / rate limit reached")
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
            return AiDecision(out.optString("action", "wait").lowercase(), box, out.optString("label"), out.optString("reason"))
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
