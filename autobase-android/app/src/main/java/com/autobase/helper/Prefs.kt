package com.autobase.helper

import android.content.Context
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object Prefs {
    private fun p(ctx: Context) = ctx.getSharedPreferences("bot", Context.MODE_PRIVATE)

    fun gamePackage(ctx: Context): String = p(ctx).getString("pkg", "") ?: ""
    fun setGamePackage(ctx: Context, v: String) = p(ctx).edit().putString("pkg", v.trim()).apply()

    fun intervalSec(ctx: Context): Int = p(ctx).getInt("interval", 30).coerceAtLeast(5)
    fun setIntervalSec(ctx: Context, v: Int) = p(ctx).edit().putInt("interval", v).apply()

    // ---- AI mode ----
    fun aiMode(ctx: Context): Boolean = p(ctx).getBoolean("ai", false)
    fun setAiMode(ctx: Context, v: Boolean) = p(ctx).edit().putBoolean("ai", v).apply()

    fun apiKey(ctx: Context): String = p(ctx).getString("apikey", "") ?: ""
    fun setApiKey(ctx: Context, v: String) = p(ctx).edit().putString("apikey", v.trim()).apply()

    fun model(ctx: Context): String = p(ctx).getString("model", "claude-haiku-5-5") ?: "claude-haiku-5-5"
    fun setModel(ctx: Context, v: String) = p(ctx).edit().putString("model", v.trim()).apply()

    fun maxCallsPerDay(ctx: Context): Int = p(ctx).getInt("maxcalls", 400).coerceAtLeast(1)
    fun setMaxCallsPerDay(ctx: Context, v: Int) = p(ctx).edit().putInt("maxcalls", v).apply()

    private fun today() = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

    fun callsToday(ctx: Context): Int {
        val sp = p(ctx)
        return if (sp.getString("day", "") == today()) sp.getInt("calls", 0) else 0
    }

    fun addCall(ctx: Context) {
        p(ctx).edit().putString("day", today()).putInt("calls", callsToday(ctx) + 1).apply()
    }
}
