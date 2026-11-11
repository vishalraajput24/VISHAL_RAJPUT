package com.autobase.helper

import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Small in-memory + on-disk log so problems can be copied out of the phone and diagnosed. */
object BotLog {
    private val lines = ArrayDeque<String>()
    private val fmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private var file: File? = null
    @Volatile var header: String = ""

    @Synchronized
    fun init(dir: File) {
        if (file != null) return
        val f = File(dir, "botlog.txt")
        file = f
        runCatching {
            if (f.exists()) {
                if (f.length() > 200_000) f.writeText(f.readLines().takeLast(300).joinToString("\n") + "\n")
                f.readLines().takeLast(300).forEach { lines.addLast(it) }
            }
        }
    }

    @Synchronized
    fun add(msg: String) {
        // noisy countdown messages are not useful in a log
        if (msg.startsWith("Detecting game...") || msg.startsWith("Downloading update")) return
        if (lines.isNotEmpty() && lines.last().length > 9 && lines.last().substring(9) == msg) return
        val line = fmt.format(Date()) + " " + msg
        lines.addLast(line)
        while (lines.size > 300) lines.removeFirst()
        runCatching { file?.appendText(line + "\n") }
    }

    @Synchronized
    fun dump(last: Int = 80): String =
        (if (header.isNotEmpty()) header + "\n" else "") + lines.toList().takeLast(last).joinToString("\n")
}
