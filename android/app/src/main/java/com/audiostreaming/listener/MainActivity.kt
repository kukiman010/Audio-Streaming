package com.audiostreaming.listener

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.LayoutInflater
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.lifecycleScope
import com.audiostreaming.listener.databinding.ActivityMainBinding
import com.audiostreaming.listener.databinding.DialogPortsBinding
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var preferences: SessionPreferences
    private var isConnected = false
    private var isConnecting = false
    private var isPaused = false

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) {
                Toast.makeText(this, "Notifications permission helps keep playback in background.", Toast.LENGTH_LONG).show()
            }
        }

    private val qrScanLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode != RESULT_OK) {
                return@registerForActivityResult
            }
            val host = result.data?.getStringExtra(QrScanActivity.EXTRA_HOST).orEmpty()
            val helperPort = result.data?.getIntExtra(
                QrScanActivity.EXTRA_HELPER_PORT,
                ConnectionConfig.DEFAULT_HELPER_PORT,
            ) ?: ConnectionConfig.DEFAULT_HELPER_PORT
            if (host.isBlank()) {
                setStatus(getString(R.string.qr_invalid))
                return@registerForActivityResult
            }
            binding.serverIpInput.setText(host)
            preferences.save(
                preferences.load().copy(
                    serverHost = host,
                    helperPort = helperPort,
                ),
            )
            setStatus("QR loaded")
            connect()
        }

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != PlaybackService.ACTION_STATUS_CHANGED) {
                return
            }
            val status = intent.getStringExtra(PlaybackService.EXTRA_STATUS).orEmpty()
            val message = intent.getStringExtra(PlaybackService.EXTRA_STATUS_MESSAGE).orEmpty()
            updateUiForStatus(status, message)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        applyWindowInsetsPadding()

        preferences = SessionPreferences(this)
        restoreSession()

        binding.qrScanButton.setOnClickListener { launchQrScanner() }
        binding.portsButton.setOnClickListener { showPortsDialog() }
        binding.actionButton.setOnClickListener { onActionButtonClicked() }

        binding.serverIpInput.doAfterTextChanged {
            preferences.save(currentSession().copy(serverHost = it?.toString().orEmpty().trim()))
        }

        updateActionButton()
        requestNotificationPermissionIfNeeded()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(PlaybackService.ACTION_STATUS_CHANGED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(statusReceiver, filter)
        }
    }

    override fun onStop() {
        unregisterReceiver(statusReceiver)
        super.onStop()
    }

    private fun restoreSession() {
        binding.serverIpInput.setText(preferences.load().serverHost)
        setStatus(getString(R.string.status_offline))
    }

    private fun currentSession(): SessionPreferences.SessionConfig = preferences.load().copy(
        serverHost = binding.serverIpInput.text?.toString().orEmpty().trim(),
    )

    private fun onActionButtonClicked() {
        if (isConnected || isConnecting) {
            disconnect()
        } else {
            connect()
        }
    }

    private fun connect() {
        val session = currentSession()
        if (session.serverHost.isBlank()) {
            setStatus(getString(R.string.invalid_ip))
            return
        }

        preferences.save(session)
        requestNotificationPermissionIfNeeded()

        isConnecting = true
        isPaused = false
        binding.waveformView.reset()
        setInputsEnabled(false)
        updateActionButton()
        setStatus(getString(R.string.connecting))

        lifecycleScope.launch {
            try {
                val discovery = withContext(Dispatchers.IO) {
                    HelperApi.fetchDiscovery(session.helperUrl())
                }
                val params = ConnectionConfig.resolveForConnect(session, discovery)
                val intent = PlaybackService.connectIntent(
                    context = this@MainActivity,
                    helperUrl = params.helperUrl,
                    livekitUrl = params.livekitUrl,
                    room = params.room,
                    identity = params.identity,
                )
                ContextCompat.startForegroundService(this@MainActivity, intent)
            } catch (error: Exception) {
                isConnecting = false
                setInputsEnabled(true)
                updateActionButton()
                setStatus("error: ${error.message}")
            }
        }
    }

    private fun disconnect() {
        startService(PlaybackService.disconnectIntent(this))
        isConnected = false
        isConnecting = false
        isPaused = false
        binding.waveformView.reset()
        setInputsEnabled(true)
        updateActionButton()
        setStatus(getString(R.string.status_offline))
    }

    private fun launchQrScanner() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
            return
        }
        qrScanLauncher.launch(Intent(this, QrScanActivity::class.java))
    }

    private val cameraPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                qrScanLauncher.launch(Intent(this, QrScanActivity::class.java))
            } else {
                Toast.makeText(this, R.string.camera_permission_required, Toast.LENGTH_LONG).show()
            }
        }

    private fun showPortsDialog() {
        val session = preferences.load()
        val dialogBinding = DialogPortsBinding.inflate(LayoutInflater.from(this))
        dialogBinding.helperPortInput.setText(session.helperPort.toString())
        dialogBinding.livekitPortInput.setText(session.livekitPort.toString())

        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.ports_settings)
            .setView(dialogBinding.root)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                val helperPort = dialogBinding.helperPortInput.text?.toString()?.toIntOrNull()
                val livekitPort = dialogBinding.livekitPortInput.text?.toString()?.toIntOrNull()
                if (helperPort == null || livekitPort == null || !isValidPort(helperPort) || !isValidPort(livekitPort)) {
                    Toast.makeText(this, R.string.invalid_port, Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                preferences.save(
                    session.copy(
                        serverHost = currentSession().serverHost,
                        helperPort = helperPort,
                        livekitPort = livekitPort,
                    ),
                )
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun isValidPort(port: Int): Boolean = port in 1..65535

    private fun updateUiForStatus(status: String, message: String) {
        when (status) {
            PlaybackService.STATUS_CONNECTING -> {
                isConnecting = true
                isConnected = false
                isPaused = false
                setInputsEnabled(false)
                setStatus(message)
                updateActionButton()
            }

            PlaybackService.STATUS_CONNECTED, PlaybackService.STATUS_AUDIO -> {
                isConnecting = false
                isConnected = true
                isPaused = false
                setInputsEnabled(false)
                setStatus(message)
                updateActionButton()
                binding.waveformView.start()
            }

            PlaybackService.STATUS_PAUSED -> {
                isPaused = true
                setStatus(message)
                updateActionButton()
                binding.waveformView.stop()
            }

            PlaybackService.STATUS_PLAYING -> {
                isPaused = false
                setStatus(message)
                updateActionButton()
                binding.waveformView.start()
            }

            PlaybackService.STATUS_DISCONNECTED -> {
                isConnecting = false
                isConnected = false
                isPaused = false
                binding.waveformView.reset()
                setInputsEnabled(true)
                setStatus(getString(R.string.status_offline))
                updateActionButton()
            }

            PlaybackService.STATUS_ERROR -> {
                isConnecting = false
                isConnected = false
                isPaused = false
                binding.waveformView.reset()
                setInputsEnabled(true)
                setStatus("error: $message")
                updateActionButton()
            }
        }
    }

    private fun setInputsEnabled(enabled: Boolean) {
        binding.serverIpInput.isEnabled = enabled
        binding.qrScanButton.isEnabled = enabled
        binding.portsButton.isEnabled = enabled
    }

    private fun updateActionButton() {
        binding.actionButton.isEnabled = !isConnecting
        binding.actionButton.text = when {
            isConnecting -> getString(R.string.connecting)
            isConnected -> getString(R.string.disconnect)
            else -> getString(R.string.connect)
        }
    }

    private fun setStatus(message: String) {
        binding.statusText.text = getString(R.string.app_version_label, BuildConfig.VERSION_NAME, message)
    }

    private fun applyWindowInsetsPadding() {
        val basePaddingPx = 16.dp
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(
                basePaddingPx,
                bars.top + basePaddingPx,
                basePaddingPx,
                bars.bottom + basePaddingPx,
            )
            insets
        }
        ViewCompat.requestApplyInsets(binding.root)
    }

    private val Int.dp: Int
        get() = (this * resources.displayMetrics.density).toInt()

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
    }
}
