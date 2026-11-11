package com.autobase.helper

import android.content.Context

object Prefs {
    private fun p(ctx: Context) = ctx.getSharedPreferences("bot", Context.MODE_PRIVATE)

    fun gamePackage(ctx: Context): String = p(ctx).getString("pkg", "") ?: ""
    fun setGamePackage(ctx: Context, v: String) = p(ctx).edit().putString("pkg", v.trim()).apply()

    fun intervalSec(ctx: Context): Int = p(ctx).getInt("interval", 30).coerceAtLeast(5)
    fun setIntervalSec(ctx: Context, v: Int) = p(ctx).edit().putInt("interval", v).apply()
}
