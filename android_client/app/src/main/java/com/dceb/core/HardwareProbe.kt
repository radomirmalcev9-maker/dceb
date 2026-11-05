package com.dceb.core

import android.content.Context
import android.hardware.ConsumerIrManager

/** Snapshot of what this device can do hardware-wise. */
data class HardwareStatus(
    val root: Boolean,
    val irBlaster: Boolean,
    val irFrequencies: List<Pair<Int, Int>>,
    val configFsUsbGadget: Boolean,
    val hidgDevices: List<String>,
    val otgSerialDevices: List<String>,
    val nfc: Boolean,
) {
    val summary: String get() = buildString {
        appendLine("root: ${if (root) "OK" else "NO"}")
        appendLine("IR blaster: ${if (irBlaster) "OK ${irFrequencies.size} ranges" else "absent"}")
        appendLine("ConfigFS USB gadget: ${if (configFsUsbGadget) "OK" else "NO"}")
        appendLine("HID gadget nodes: ${hidgDevices.joinToString().ifEmpty { "none" }}")
        appendLine("OTG serial: ${otgSerialDevices.joinToString().ifEmpty { "none" }}")
        appendLine("NFC: ${if (nfc) "OK" else "absent"}")
    }
}

object HardwareProbe {

    suspend fun probe(context: Context): HardwareStatus {
        val root = RootShell.isRootAvailable
        val cir = context.getSystemService(Context.CONSUMER_IR_SERVICE) as? ConsumerIrManager
        val irOk = cir?.hasIrEmitter() == true
        val ranges = cir?.carrierFrequencies?.map { it.minFrequency to it.maxFrequency } ?: emptyList()

        val configFs = if (root) {
            RootShell.exec("test -d /config/usb_gadget && echo yes").stdout.trim() == "yes"
        } else false

        val hidg = if (root) {
            RootShell.exec("ls /dev/hidg* 2>/dev/null").stdout.lineSequence()
                .map { it.trim() }.filter { it.isNotEmpty() }.toList()
        } else emptyList()

        val serial = if (root) {
            RootShell.exec("ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null").stdout.lineSequence()
                .map { it.trim() }.filter { it.isNotEmpty() }.toList()
        } else emptyList()

        val nfc = context.packageManager.hasSystemFeature("android.hardware.nfc")
        return HardwareStatus(root, irOk, ranges, configFs, hidg, serial, nfc)
    }
}
