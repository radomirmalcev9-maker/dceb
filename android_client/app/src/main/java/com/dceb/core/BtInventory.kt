package com.dceb.core

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Passive Bluetooth inventory for the OWNER's own environment.
 *
 * Observation only: this lists what is advertising nearby (name, MAC, RSSI) and
 * optionally the devices already bonded to this phone. It never pairs, never
 * connects, never reads data off a device and never attempts authentication —
 * touching someone else's device needs its owner's consent.
 */
data class BtDevice(
    val name: String?,
    val address: String,
    val rssi: Int? = null,
    val type: String = "LE",          // "LE" | "BONDED"
    val bondState: Int? = null,
)

object BtInventory {

    /** Permission needed to scan: BLUETOOTH_SCAN on Android 12+, BLUETOOTH_ADMIN below. */
    fun scanPermission(): String =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) Manifest.permission.BLUETOOTH_SCAN
        else Manifest.permission.BLUETOOTH_ADMIN

    /** Optional: lets us name devices already paired with this phone. */
    fun connectPermission(): String? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) Manifest.permission.BLUETOOTH_CONNECT
        else null

    fun hasScanPermission(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, scanPermission()) ==
            PackageManager.PERMISSION_GRANTED

    fun adapter(context: Context): BluetoothAdapter? =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    fun bluetoothEnabled(context: Context): Boolean = adapter(context)?.isEnabled == true

    @SuppressLint("MissingPermission")
    private fun safeName(device: BluetoothDevice): String? =
        try { device.name } catch (e: SecurityException) { null }

    /** BLE advertising scan. Results are de-duplicated by MAC address. */
    @SuppressLint("MissingPermission")
    suspend fun scanLe(context: Context, timeoutMs: Long = 5000L): List<BtDevice> =
        withContext(Dispatchers.IO) {
            val scanner = adapter(context)?.bluetoothLeScanner
                ?: return@withContext emptyList()
            if (!hasScanPermission(context)) return@withContext emptyList()

            val found = Collections.synchronizedMap(mutableMapOf<String, BtDevice>())
            val latch = CountDownLatch(1)
            val callback = object : ScanCallback() {
                override fun onScanResult(callbackType: Int, result: ScanResult) {
                    val device = result.device ?: return
                    found[device.address] = BtDevice(
                        name = result.scanRecord?.deviceName ?: safeName(device),
                        address = device.address,
                        rssi = result.rssi,
                        type = "LE",
                    )
                }

                override fun onScanFailed(errorCode: Int) = latch.countDown()
            }

            val settings = ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                .build()
            runCatching { scanner.startScan(null, settings, callback) }
                .onFailure { return@withContext emptyList() }
            latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            runCatching { scanner.stopScan(callback) }
            found.values.toList()
        }

    /** Devices already bonded to this phone; empty when CONNECT was not granted. */
    @SuppressLint("MissingPermission")
    fun bonded(context: Context): List<BtDevice> {
        val connect = connectPermission()
        if (connect != null &&
            ContextCompat.checkSelfPermission(context, connect) !=
            PackageManager.PERMISSION_GRANTED) return emptyList()
        return try {
            adapter(context)?.bondedDevices?.map { d ->
                BtDevice(name = safeName(d), address = d.address, type = "BONDED",
                         bondState = d.bondState)
            } ?: emptyList()
        } catch (e: SecurityException) {
            emptyList()
        }
    }

    /** Render the inventory as the text payload the server expects. */
    fun toDump(devices: List<BtDevice>): String = devices.joinToString("\n") { d ->
        "device: ${d.name ?: "unknown"}, ${d.address}, rssi ${d.rssi ?: "?"}"
    }
}
