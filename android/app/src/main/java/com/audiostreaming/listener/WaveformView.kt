package com.audiostreaming.listener

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.Choreographer
import android.view.View
import androidx.core.content.ContextCompat
import kotlin.math.abs
import kotlin.math.min

class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {
    private val displaySamples = FloatArray(MAX_SAMPLES)
    private var running = false
    private val choreographer = Choreographer.getInstance()
    private val backgroundPaint = Paint().apply {
        style = Paint.Style.FILL
        color = ContextCompat.getColor(context, R.color.waveform_background)
    }
    private val linePaint = Paint().apply {
        style = Paint.Style.STROKE
        strokeWidth = 1f
        color = ContextCompat.getColor(context, R.color.waveform_accent)
    }
    private val backgroundRect = RectF()
    private val cornerRadius = 12f * resources.displayMetrics.density

    private val frameCallback = object : Choreographer.FrameCallback {
        override fun doFrame(frameTimeNanos: Long) {
            if (running) {
                invalidate()
                choreographer.postFrameCallback(this)
            }
        }
    }

    fun start() {
        if (running) {
            return
        }
        running = true
        choreographer.postFrameCallback(frameCallback)
    }

    fun stop() {
        running = false
        choreographer.removeFrameCallback(frameCallback)
    }

    fun reset() {
        stop()
        WaveformSampleBuffer.clear()
        displaySamples.fill(0f)
        invalidate()
    }

    override fun onDetachedFromWindow() {
        stop()
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val viewWidth = width
        val viewHeight = height
        if (viewWidth <= 0 || viewHeight <= 0) {
            return
        }

        backgroundRect.set(0f, 0f, viewWidth.toFloat(), viewHeight.toFloat())
        canvas.drawRoundRect(backgroundRect, cornerRadius, cornerRadius, backgroundPaint)

        val columnCount = min(viewWidth, MAX_SAMPLES)
        val sampleCount = WaveformSampleBuffer.copyRecentTo(displaySamples)
        val centerY = viewHeight / 2f
        val halfHeight = viewHeight * 0.46f
        val rightOffset = columnCount - sampleCount

        for (x in 0 until columnCount) {
            val bufferIndex = x - rightOffset
            if (bufferIndex < 0) {
                continue
            }
            val amplitude = abs(displaySamples[bufferIndex]) * halfHeight
            if (amplitude < 0.5f) {
                continue
            }
            val top = centerY - amplitude
            val bottom = centerY + amplitude
            canvas.drawLine(x.toFloat(), top, x.toFloat(), bottom, linePaint)
        }
    }

    companion object {
        private const val MAX_SAMPLES = 1024
    }
}
