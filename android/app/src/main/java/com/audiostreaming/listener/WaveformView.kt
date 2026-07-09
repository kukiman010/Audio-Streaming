package com.audiostreaming.listener

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat
import kotlin.math.max
import kotlin.math.min

class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {
    private val amplitudes = FloatArray(BUFFER_SIZE)
    private var writeIndex = 0
    private var smoothedLevel = 0f
    private val barHeights = FloatArray(BAR_COUNT)
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }
    private val backgroundPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = ContextCompat.getColor(context, R.color.waveform_background)
    }
    private val barRect = RectF()
    private val backgroundRect = RectF()
    private val accentColor = ContextCompat.getColor(context, R.color.waveform_accent)
    private val accentDimColor = ContextCompat.getColor(context, R.color.waveform_bar_dim)

    fun pushLevel(level: Float) {
        val clamped = level.coerceIn(0f, 1f)
        smoothedLevel = smoothedLevel * SMOOTHING + clamped * (1f - SMOOTHING)
        amplitudes[writeIndex] = smoothedLevel
        writeIndex = (writeIndex + 1) % BUFFER_SIZE
        postInvalidateOnAnimation()
    }

    fun reset() {
        smoothedLevel = 0f
        writeIndex = 0
        amplitudes.fill(0f)
        barHeights.fill(0f)
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val width = width.toFloat()
        val height = height.toFloat()
        if (width <= 0f || height <= 0f) {
            return
        }

        val cornerRadius = 12f * resources.displayMetrics.density
        backgroundRect.set(0f, 0f, width, height)
        canvas.drawRoundRect(backgroundRect, cornerRadius, cornerRadius, backgroundPaint)

        val samplesPerBar = max(1, BUFFER_SIZE / BAR_COUNT)
        for (bar in 0 until BAR_COUNT) {
            var peak = 0f
            val start = bar * samplesPerBar
            val end = min(BUFFER_SIZE, start + samplesPerBar)
            for (sample in start until end) {
                val index = (writeIndex + sample) % BUFFER_SIZE
                peak = max(peak, amplitudes[index])
            }
            barHeights[bar] = barHeights[bar] * BAR_SMOOTHING + peak * (1f - BAR_SMOOTHING)
        }

        val gap = width * 0.018f
        val totalGap = gap * (BAR_COUNT - 1)
        val barWidth = (width - totalGap) / BAR_COUNT
        val minBarHeight = height * MIN_BAR_RATIO
        val maxBarHeight = height * 0.88f

        for (bar in 0 until BAR_COUNT) {
            val left = bar * (barWidth + gap)
            val right = left + barWidth
            val normalized = barHeights[bar].coerceIn(0f, 1f)
            val barHeight = minBarHeight + normalized * (maxBarHeight - minBarHeight)
            val top = height - barHeight
            barRect.set(left, top, right, height)

            barPaint.shader = LinearGradient(
                0f,
                top,
                0f,
                height,
                accentColor,
                accentDimColor,
                Shader.TileMode.CLAMP,
            )
            val radius = min(barWidth / 2f, 6f * resources.displayMetrics.density)
            canvas.drawRoundRect(barRect, radius, radius, barPaint)
        }
        barPaint.shader = null
    }

    companion object {
        private const val BUFFER_SIZE = 120
        private const val BAR_COUNT = 36
        private const val SMOOTHING = 0.72f
        private const val BAR_SMOOTHING = 0.55f
        private const val MIN_BAR_RATIO = 0.04f
    }
}
