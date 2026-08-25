package com.audiostreaming.listener

object WaveformSampleBuffer {
    private const val CAPACITY = 1024

    private val samples = FloatArray(CAPACITY)
    private var writeIndex = 0
    private var size = 0
    private val lock = Any()

    fun write(sample: Float) {
        synchronized(lock) {
            samples[writeIndex] = sample.coerceIn(-1f, 1f)
            writeIndex = (writeIndex + 1) % CAPACITY
            if (size < CAPACITY) {
                size++
            }
        }
    }

    fun write(source: FloatArray, count: Int) {
        if (count <= 0) {
            return
        }
        synchronized(lock) {
            val limit = minOf(count, source.size)
            for (index in 0 until limit) {
                samples[writeIndex] = source[index].coerceIn(-1f, 1f)
                writeIndex = (writeIndex + 1) % CAPACITY
                if (size < CAPACITY) {
                    size++
                }
            }
        }
    }

    /**
     * Copies the most recent samples into [dest], oldest first (left-to-right on screen).
     * Returns the number of samples written.
     */
    fun copyRecentTo(dest: FloatArray): Int {
        synchronized(lock) {
            val copyCount = minOf(size, dest.size)
            if (copyCount == 0) {
                dest.fill(0f)
                return 0
            }
            val start = (writeIndex - copyCount + CAPACITY) % CAPACITY
            if (start + copyCount <= CAPACITY) {
                System.arraycopy(samples, start, dest, 0, copyCount)
            } else {
                val firstPart = CAPACITY - start
                System.arraycopy(samples, start, dest, 0, firstPart)
                System.arraycopy(samples, 0, dest, firstPart, copyCount - firstPart)
            }
            if (copyCount < dest.size) {
                dest.fill(0f, copyCount, dest.size)
            }
            return copyCount
        }
    }

    fun clear() {
        synchronized(lock) {
            samples.fill(0f)
            writeIndex = 0
            size = 0
        }
    }
}
