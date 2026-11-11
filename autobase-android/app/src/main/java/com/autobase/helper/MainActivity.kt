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

        add(text("Step 1: enable the AutoBase accessibility service."))
        add(button("Open Accessibility settings") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
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

        add(text("Check interval when idle (seconds)"))
        intervalField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(Prefs.intervalSec(this@MainActivity).toString())
        }
        add(intervalField)

        add(button("START") {
            Prefs.setGamePackage(this, pkgField.text.toString())
            Prefs.setIntervalSec(this, intervalField.text.toString().toIntOrNull() ?: 30)
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
