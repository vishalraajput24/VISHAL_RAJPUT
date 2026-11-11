package com.autobase.helper

import com.anthropic.client.AnthropicClient
import com.anthropic.client.okhttp.AnthropicOkHttpClient
import com.anthropic.core.JsonValue
import com.anthropic.models.messages.Base64ImageSource
import com.anthropic.models.messages.ContentBlockParam
import com.anthropic.models.messages.ImageBlockParam
import com.anthropic.models.messages.MessageCreateParams
import com.anthropic.models.messages.MessageParam
import com.anthropic.models.messages.OutputConfig
import com.anthropic.models.messages.TextBlockParam
import com.anthropic.models.messages.Tool
import kotlin.jvm.optionals.getOrNull

/** What the model decided. box is [x1, y1, x2, y2] in pixels of the image it was shown. */
data class AiDecision(
    val action: String,
    val box: List<Int>?,
    val label: String,
    val reason: String,
    val waitSec: Int? = null
)

/** Thrown when a provider says "too many requests / quota used up". */
class RateLimitException(message: String) : Exception(message)

/** Thrown when the provider is temporarily overloaded (HTTP 500/502/503/504). Worth retrying later. */
class OverloadedException(message: String) : Exception(message)

interface Brain {
    fun decide(jpegBase64: String, width: Int, height: Int, history: List<String>): AiDecision
}

object AiPrompts {
    fun history(history: List<String>) =
        if (history.isEmpty()) "none yet" else history.joinToString("\n") { "- $it" }

    val SYSTEM = """
You operate the mobile strategy game Kingshot on the owner's own phone, looking at one screenshot at a time.
Your job is to keep the city progressing while the owner sleeps, safely.

Do, in this priority order:
1. Dismiss harmless popups: "Welcome back!" (tap Confirm), info dialogs (tap their X / Confirm / Claim).
2. Collect what is ready: resource bubbles above buildings, free daily/idle rewards, completed-task Claim buttons.
3. Alliance Help: if a "Help" / "Help All" button is visible, tap it.
4. Keep the building queue busy: the quick panel (Town tab) lists "Building Queue 1 - Idle" with an arrow button. If a queue is Idle, use the arrow to go to a building and start an Upgrade that costs only normal resources (food, wood, stone, iron, coins). Prefer the building named in the quest bar at the bottom (e.g. "Upgrade Town Center to Lv. 15"). The Upgrade screen needs a final Upgrade/Confirm tap.
If none of this applies, or everything is already running, choose wait.

Never tap anything that could spend real money or premium currency, or is irreversible:
Shop, Deals, Top-up, VIP purchase, Buy, any button showing a price or a gem/diamond cost, Speed Up or Use Item buttons,
attack / rally / march / teleport / dismiss troops, chat, settings, account, mail deletion.
If an Upgrade screen would use gems, or you are unsure what a button does, choose wait (or stop if the screen looks wrong).

Rules:
- Return exactly one action: tap, wait, or stop.
- For tap, give a tight box around the button you want.
- Do not repeat an action that did not change the screen; if your last actions look stuck, choose stop.
- Loading screens, animations or unclear screens: wait.
- When you choose wait, also give wait_seconds: if a build/training/research timer is the only thing pending, use about that remaining time but at most 300; on a loading or transition screen use 10; otherwise use 60.
""".trimIndent()
}

class ClaudeAgent(apiKey: String, private val model: String) : Brain {

    private val client: AnthropicClient = AnthropicOkHttpClient.builder().apiKey(apiKey).build()

    private fun prop(type: String, desc: String, enum: List<String>? = null): JsonValue {
        val m = linkedMapOf<String, Any>("type" to type, "description" to desc)
        if (enum != null) m["enum"] = enum
        return JsonValue.from(m)
    }

    private val actTool: Tool = Tool.builder()
        .name("act")
        .description("Report the single next action to perform on the phone screen.")
        .inputSchema(
            Tool.InputSchema.builder()
                .properties(
                    Tool.InputSchema.Properties.builder()
                        .putAdditionalProperty("action", prop("string", "tap = tap the centre of box; wait = nothing useful to do right now; stop = something looks wrong or risky, hand control back to the human", listOf("tap", "wait", "stop")))
                        .putAdditionalProperty("x1", prop("integer", "left edge of the target button in image pixels (only for tap)"))
                        .putAdditionalProperty("y1", prop("integer", "top edge of the target button in image pixels (only for tap)"))
                        .putAdditionalProperty("x2", prop("integer", "right edge of the target button in image pixels (only for tap)"))
                        .putAdditionalProperty("y2", prop("integer", "bottom edge of the target button in image pixels (only for tap)"))
                        .putAdditionalProperty("label", prop("string", "the visible text or short name of the button you tap, e.g. Confirm, Help, Upgrade"))
                        .putAdditionalProperty("reason", prop("string", "one short sentence: why this action"))
                        .putAdditionalProperty("wait_seconds", prop("integer", "only for wait: seconds to sleep before looking again (10-300)"))
                        .build()
                )
                .required(listOf("action", "reason"))
                .build()
        )
        .build()

    /** Blocking call, run it off the main thread. Throws on network/API errors. */
    override fun decide(jpegBase64: String, width: Int, height: Int, history: List<String>): AiDecision {
        val histText = AiPrompts.history(history)
        val prompt = "Screenshot size: ${width}x${height} pixels (box coordinates must use this size).\n" +
            "Your last actions (oldest first):\n$histText\n\nDecide the next action and call the act tool."

        val content = listOf(
            ContentBlockParam.ofImage(
                ImageBlockParam.builder()
                    .source(
                        Base64ImageSource.builder()
                            .data(jpegBase64)
                            .mediaType(Base64ImageSource.MediaType.IMAGE_JPEG)
                            .build()
                    )
                    .build()
            ),
            ContentBlockParam.ofText(TextBlockParam.builder().text(prompt).build())
        )

        val params = MessageCreateParams.builder()
            .model(model)
            .maxTokens(2000L)
            .system(AiPrompts.SYSTEM + "\nAlways call the act tool exactly once. Box coordinates are pixels of the screenshot you were shown.")
            .addTool(actTool)
            .outputConfig(OutputConfig.builder().effort(OutputConfig.Effort.LOW).build())
            .addMessage(MessageParam.ofUserOfContentBlockParams(content))
            .build()

        val response = client.messages().create(params)

        for (block in response.content()) {
            val tu = block.toolUse().getOrNull() ?: continue
            val input = tu._input().asObject().getOrNull() ?: continue
            fun str(k: String): String = input[k]?.asString()?.getOrNull() ?: ""
            fun num(k: String): Int? = input[k]?.asNumber()?.getOrNull()?.toInt()
            val action = str("action").lowercase()
            val x1 = num("x1"); val y1 = num("y1"); val x2 = num("x2"); val y2 = num("y2")
            val box = if (x1 != null && y1 != null && x2 != null && y2 != null) listOf(x1, y1, x2, y2) else null
            return AiDecision(action, box, str("label"), str("reason"), num("wait_seconds"))
        }
        return AiDecision("wait", null, "", "no tool call returned")
    }
}
