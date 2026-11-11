package com.autobase.helper

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import org.opencv.android.Utils
import org.opencv.core.Core
import org.opencv.core.Mat
import org.opencv.core.Size
import org.opencv.imgproc.Imgproc
import java.io.File

/** The buttons the bot looks for, in priority order. Add a name here to support another button. */
val BUTTON_NAMES = listOf("collect", "help", "upgrade")

/** Matching runs on a half-size grayscale image: much faster, accuracy is fine for UI buttons. */
const val SCALE = 0.5

object Templates {
    private fun dir(ctx: Context) = File(ctx.filesDir, "templates").also { it.mkdirs() }
    fun file(ctx: Context, name: String) = File(dir(ctx), "$name.png")
    fun exists(ctx: Context, name: String) = file(ctx, name).exists()

    fun save(ctx: Context, name: String, bmp: Bitmap) {
        file(ctx, name).outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    /** Loads every saved template as a preprocessed gray Mat. */
    fun loadAll(ctx: Context): Map<String, Mat> {
        val out = LinkedHashMap<String, Mat>()
        for (n in BUTTON_NAMES) {
            val f = file(ctx, n)
            if (!f.exists()) continue
            val bmp = BitmapFactory.decodeFile(f.absolutePath) ?: continue
            out[n] = prepare(bmp)
        }
        return out
    }

    fun prepare(bmp: Bitmap): Mat {
        val sw = if (bmp.config == Bitmap.Config.ARGB_8888) bmp else bmp.copy(Bitmap.Config.ARGB_8888, false)
        val rgba = Mat()
        Utils.bitmapToMat(sw, rgba)
        val gray = Mat()
        Imgproc.cvtColor(rgba, gray, Imgproc.COLOR_RGBA2GRAY)
        val small = Mat()
        Imgproc.resize(gray, small, Size(), SCALE, SCALE, Imgproc.INTER_AREA)
        rgba.release(); gray.release()
        return small
    }

    /** Returns the centre of the best match in full-resolution screen pixels, or null. */
    fun find(screen: Mat, template: Mat, threshold: Double): Pair<Int, Int>? {
        if (template.cols() > screen.cols() || template.rows() > screen.rows()) return null
        val result = Mat()
        Imgproc.matchTemplate(screen, template, result, Imgproc.TM_CCOEFF_NORMED)
        val mm = Core.minMaxLoc(result)
        result.release()
        if (mm.maxVal < threshold) return null
        val cx = (mm.maxLoc.x + template.cols() / 2.0) / SCALE
        val cy = (mm.maxLoc.y + template.rows() / 2.0) / SCALE
        return Pair(cx.toInt(), cy.toInt())
    }
}
