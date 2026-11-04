package com.dceb.otg

import com.dceb.core.RootShell

/**
 * Bridge for external USB-UART radio/RFID modules (CC1101, ESP32, etc.)
 * exposed as /dev/ttyUSB* or /dev/ttyACM*.
 */
object UsbSerialBridge {

    suspend fun listDevices(): List<String> =
        RootShell.exec("ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null").stdout.lineSequence()
            .map { it.trim() }.filter { it.isNotEmpty() }.toList()

    suspend fun send(device: String, baud: Int, payloadHex: String): Boolean {
        // stty configures the line, then bytes are pushed with printf octal escapes.
        val bytes = payloadHex.chunked(2).map { it.toInt(16).toByte() }
        val octal = bytes.joinToString("") { "\\%03o".format(it.toInt() and 0xFF) }
        val r = RootShell.exec("sh", "-c",
            "stty -F $device $baud raw -echo && printf '$octal' > $device")
        return r.ok
    }

    suspend fun read(device: String, baud: Int, timeoutSec: Int = 3): String {
        val r = RootShell.exec("sh", "-c",
            "stty -F $device $baud raw -echo && timeout $timeoutSec cat $device | od -An -tx1 | tr -d ' \\n'")
        return r.stdout.trim()
    }
}
