package com.autobase.helper

import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.Switch
import android.app.AlertDialog
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts

class MainActivity : ComponentActivity() {

    private val ui = Handler(Looper.getMainLooper())
    private lateinit var statusView: TextView
    private val slotViews = HashMap<String, TextView>()
    private var pendingName: String? = null
    private lateinit var pkgField: EditText
    private lateinit var intervalField: EditText
    private lateinit var aiSwitch: Switch
    private lateinit var apiKeyField: EditText
    private lateinit var modelField: EditText
    private lateinit var geminiKeyField: EditText
    private lateinit var geminiModelField: EditText
    private lateinit var rbGemini: RadioButton
    private lateinit var rbClaude: RadioButton
    private lateinit var maxCallsField: EditText

    private val picker = registerForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        val name = pendingName
        if (uri == null || name == null) return@registerForActivityResult
        val bmp = contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it) }
        if (bmp == null) {
            Toast.makeText(this, "Could not read that image", Toast.LENGTH_SHORT).show()
        } else {
            Templates.save(this, name, bmp)
            refreshSlots()
        }
    }

    private val ticker = object : Runnable {
        override fun run() {
            statusView.text = "Status: " + BotService.status
            ui.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        setContentView(ScrollView(this).apply { addView(root) })

        fun text(s: String) = TextView(this).apply { text = s; setPadding(0, pad / 2, 0, pad / 2) }
        fun button(s: String, onClick: () -> Unit) = Button(this).apply {
            text = s; setOnClickListener { onClick() }
        }
        fun add(v: android.view.View) = root.addView(
            v, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        )

        statusView = text("Status: ...").apply { textSize = 16f }
        add(statusView)

        add(text("Step 1: enable the Night Builder accessibility service."))
        add(button("Open Accessibility settings") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        add(text(
            "Not listed, or greyed out? Android blocks apps installed from a file. Tap the button below -> " +
                "three dots (top right) -> 'Allow restricted settings'. Then open Accessibility again: " +
                "Installed / Downloaded apps -> Night Builder -> ON."
        ))
        add(button("Open App info (Allow restricted settings)") {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))
            )
        })

        add(text(
            "Step 2: for each button, take a screenshot in the game, crop ONLY that button " +
                "in your gallery editor, then pick the cropped image here."
        ))
        for (n in BUTTON_NAMES) {
            val label = TextView(this)
            slotViews[n] = label
            add(label)
            add(button("Choose image for: $n") {
                pendingName = n
                picker.launch("image/*")
            })
        }

        add(text("Step 3 (optional): game package name. Taps only happen while this app is in front. Leave empty to skip."))
        pkgField = EditText(this).apply { setText(Prefs.gamePackage(this@MainActivity)) }
        add(pkgField)
        add(button("Detect game package (open the game within 6 seconds)") {
            Toast.makeText(this, "Open the game now. Detecting in 6 seconds...", Toast.LENGTH_LONG).show()
            ui.postDelayed({
                val p = BotService.instance?.currentPackage()
                if (p != null && p != packageName) {
                    pkgField.setText(p)
                    Toast.makeText(this, "Detected: $p", Toast.LENGTH_LONG).show()
                } else {
                    Toast.makeText(this, "Could not detect. Is the accessibility service on and the game in front?", Toast.LENGTH_LONG).show()
                }
            }, 6000)
        })

        add(text("Check interval when idle (seconds)"))
        intervalField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(Prefs.intervalSec(this@MainActivity).toString())
        }
        add(intervalField)

        add(text(
            "AI mode (optional): the AI looks at the screen and decides what to tap. Needs internet. " +
                "Game screenshots are sent to the provider you pick. Requires the game package above."
        ))
        aiSwitch = Switch(this).apply {
            text = "Use AI mode"
            isChecked = Prefs.aiMode(this@MainActivity)
        }
        add(aiSwitch)

        val group = RadioGroup(this)
        rbGemini = RadioButton(this).apply { text = "Google Gemini (free key from aistudio.google.com/apikey)"; id = 1001 }
        rbClaude = RadioButton(this).apply { text = "Anthropic Claude (paid API key)"; id = 1002 }
        group.addView(rbGemini)
        group.addView(rbClaude)
        if (Prefs.provider(this) == "gemini") rbGemini.isChecked = true else rbClaude.isChecked = true
        add(group)

        add(text("Gemini API key"))
        geminiKeyField = EditText(this).apply {
            hint = "AIza..."
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(Prefs.geminiKey(this@MainActivity))
        }
        add(geminiKeyField)
        add(text("Gemini model (use 'List Gemini models' to see what your key can use)"))
        geminiModelField = EditText(this).apply { setText(Prefs.geminiModel(this@MainActivity)) }
        add(geminiModelField)
        add(button("List Gemini models") {
            val key = geminiKeyField.text.toString().trim()
            if (key.isEmpty()) {
                Toast.makeText(this, "Enter the Gemini key first", Toast.LENGTH_SHORT).show()
            } else {
                Thread {
                    val res = runCatching { GeminiAgent.listFlashModels(key) }
                    runOnUiThread {
                        val msg = res.getOrNull()?.take(20)?.joinToString("\n")
                            ?: ("Error: " + res.exceptionOrNull()?.message)
                        AlertDialog.Builder(this).setTitle("Flash models").setMessage(msg).setPositiveButton("OK", null).show()
                    }
                }.start()
            }
        })

        add(text("Claude API key (only if you use Claude)"))
        apiKeyField = EditText(this).apply {
            hint = "sk-ant-..."
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(Prefs.apiKey(this@MainActivity))
        }
        add(apiKeyField)
        add(text("Claude model"))
        modelField = EditText(this).apply { setText(Prefs.model(this@MainActivity)) }
        add(modelField)

        add(text("Max AI calls per day (cost / quota cap)"))
        maxCallsField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(Prefs.maxCallsPerDay(this@MainActivity).toString())
        }
        add(maxCallsField)

        add(button("START") {
            Prefs.setGamePackage(this, pkgField.text.toString())
            Prefs.setIntervalSec(this, intervalField.text.toString().toIntOrNull() ?: 30)
            Prefs.setAiMode(this, aiSwitch.isChecked)
            Prefs.setApiKey(this, apiKeyField.text.toString())
            Prefs.setGeminiKey(this, geminiKeyField.text.toString())
            Prefs.setGeminiModel(this, geminiModelField.text.toString())
            Prefs.setProvider(this, if (rbGemini.isChecked) "gemini" else "claude")
            Prefs.setModel(this, modelField.text.toString())
            Prefs.setMaxCallsPerDay(this, maxCallsField.text.toString().toIntOrNull() ?: 400)
            val svc = BotService.instance
            if (svc == null) {
                Toast.makeText(this, "Enable the accessibility service first", Toast.LENGTH_LONG).show()
            } else {
                svc.startBot()
                Toast.makeText(this, "Started. Now open the game.", Toast.LENGTH_LONG).show()
            }
        })
        add(button("STOP") { BotService.instance?.stopBot() })

        add(text(
            "Tips: keep the phone charging, set screen timeout to the maximum, and turn off " +
                "battery optimisation for this app. Bot auto-stops after ${BotService.MAX_HOURS} h."
        ))
    }

    override fun onResume() {
        super.onResume()
        refreshSlots()
        ui.post(ticker)
    }

    override fun onPause() {
        ui.removeCallbacks(ticker)
        super.onPause()
    }

    private fun refreshSlots() {
        for ((n, v) in slotViews) {
            v.text = (if (Templates.exists(this, n)) "[set] " else "[not set] ") + n
        }
    }
}
