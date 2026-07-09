package com.twilio.audioswitch.scanners

import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import androidx.annotation.RequiresApi
import androidx.annotation.VisibleForTesting
import com.twilio.audioswitch.AudioDevice
import com.twilio.audioswitch.android.Logger
import com.twilio.audioswitch.android.ProductionLogger
import java.util.concurrent.Executor

/**
 * Discovers devices suitable for voice communication using
 * [AudioManager.getAvailableCommunicationDevices], and refreshes when:
 * - the output device graph changes ([AudioManager.registerAudioDeviceCallback]), or
 * - the active communication routing target changes
 *   ([AudioManager.addOnCommunicationDeviceChangedListener]), which can happen without a
 *   hardware add/remove (for example when the system or another component changes routing).
 */
@RequiresApi(Build.VERSION_CODES.S)
internal class CommunicationDeviceScanner(
    private val audioManager: AudioManager,
    private val handler: Handler,
    private val logger: Logger = ProductionLogger(loggingEnabled = false),
) : AudioDeviceCallback(), Scanner {

    companion object {
        private const val TAG = "CommunicationDeviceScanner"
    }

    @VisibleForTesting(otherwise = VisibleForTesting.PRIVATE)
    internal var listener: Scanner.Listener? = null

    private val callbackExecutor = Executor { handler.post(it) }

    private val communicationDeviceChangedListener =
        AudioManager.OnCommunicationDeviceChangedListener {
            notifyCommunicationDeviceListChanged()
        }

    private var communicationDevicesById: Map<Int, AudioDeviceInfo> = emptyMap()

    override fun isDeviceActive(audioDevice: AudioDevice): Boolean =
        audioManager.getAvailableCommunicationDevices().any {
            it.toTwilioAudioDevice() == audioDevice
        }

    fun findCommunicationDeviceInfo(audioDevice: AudioDevice): AudioDeviceInfo? =
        audioManager.getAvailableCommunicationDevices().firstOrNull {
            it.toTwilioAudioDevice() == audioDevice
        }

    override fun start(listener: Scanner.Listener): Boolean {
        this.listener = listener
        audioManager.registerAudioDeviceCallback(this, handler)
        audioManager.addOnCommunicationDeviceChangedListener(
            callbackExecutor,
            communicationDeviceChangedListener
        )
        notifyCommunicationDeviceListChanged()
        return true
    }

    override fun stop(): Boolean {
        try {
            audioManager.removeOnCommunicationDeviceChangedListener(communicationDeviceChangedListener)
        } catch (e: Exception) {
            logger.e(TAG, "Failed to remove communication device changed listener", e)
        }
        try {
            audioManager.unregisterAudioDeviceCallback(this)
        } catch (e: Exception) {
            logger.e(TAG, "Failed to unregister audio device callback", e)
        }
        this.listener = null
        communicationDevicesById = emptyMap()
        return true
    }

    override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) {
        // Do not call super: JVM unit tests use android.jar stubs that throw for AudioDeviceCallback.
        notifyCommunicationDeviceListChanged()
    }

    override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) {
        notifyCommunicationDeviceListChanged()
    }

    private fun snapshotCommunicationDevicesById(): Map<Int, AudioDeviceInfo> =
        audioManager.getAvailableCommunicationDevices().associateBy { it.id }

    private fun notifyCommunicationDeviceListChanged() {
        val listener = this.listener ?: return
        val newById = snapshotCommunicationDevicesById()
        val oldById = communicationDevicesById

        for (id in oldById.keys - newById.keys) {
            oldById[id]?.toTwilioAudioDevice()?.let { listener.onDeviceDisconnected(it) }
        }
        for (id in newById.keys - oldById.keys) {
            newById[id]?.toTwilioAudioDevice()?.let { listener.onDeviceConnected(it) }
        }
        communicationDevicesById = newById
    }
}

@RequiresApi(Build.VERSION_CODES.M)
internal fun AudioDeviceInfo.toTwilioAudioDevice(): AudioDevice? =
    when {
        type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ->
            AudioDevice.BluetoothHeadset(productName.toString())
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            (type == AudioDeviceInfo.TYPE_BLE_HEADSET || type == AudioDeviceInfo.TYPE_BLE_SPEAKER) ->
            AudioDevice.BluetoothHeadset(productName.toString())
        type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
            type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES ||
            (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && type == AudioDeviceInfo.TYPE_USB_HEADSET) ->
            AudioDevice.WiredHeadset()
        type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE ->
            AudioDevice.Earpiece()
        type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER ->
            AudioDevice.Speakerphone()
        else -> null
    }
