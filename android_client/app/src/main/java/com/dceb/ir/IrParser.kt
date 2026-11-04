package com.dceb.ir

/**
 * Parser for Flipper Zero `.ir` files (Filetype: IR signals file, Version: 1).
 *
 * Supports both entry types:
 *  - `type: parsed`  -> protocol/address/command, re-encoded to a raw pattern
 *  - `type: raw`     -> frequency / duty_cycle / data[] used as-is
 *
 * Protocols encoded to mark/space microsecond patterns:
 * NEC, NECext, Samsung32, RC5, RC6, Sony SIRC (12/15/20-bit).
 */
object IrParser {

    data class IrSignal(
        val name: String,
        val protocol: String,
        val frequency: Int,
        val dutyCycle: Float,
        val pattern: IntArray,          // microseconds, alternating mark/space, mark first
    )

    data class IrFile(val signals: List<IrSignal>)

    fun parse(text: String): IrFile {
        val signals = mutableListOf<IrSignal>()
        var name = ""
        var type = ""
        var protocol = ""
        var address = ""
        var command = ""
        var frequency = 38000
        var duty = 0.33f
        var data = intArrayOf()

        fun flush() {
            if (name.isEmpty()) return
            val pattern = when (type.lowercase()) {
                "raw" -> data
                else -> encodeParsed(protocol, address, command, frequency)
            }
            if (pattern.isNotEmpty()) {
                signals += IrSignal(name, protocol.ifEmpty { "RAW" }, frequency, duty, pattern)
            }
            name = ""; type = ""; protocol = ""; address = ""; command = ""
            frequency = 38000; duty = 0.33f; data = intArrayOf()
        }

        for (rawLine in text.lineSequence()) {
            val line = rawLine.trim()
            when {
                line.startsWith("name:") -> { flush(); name = line.removePrefix("name:").trim() }
                line.startsWith("type:") -> type = line.removePrefix("type:").trim()
                line.startsWith("protocol:") -> protocol = line.removePrefix("protocol:").trim()
                line.startsWith("address:") -> address = line.removePrefix("address:").trim()
                line.startsWith("command:") -> command = line.removePrefix("command:").trim()
                line.startsWith("frequency:") ->
                    frequency = line.removePrefix("frequency:").trim().toIntOrDefault(38000)
                line.startsWith("duty_cycle:") ->
                    duty = line.removePrefix("duty_cycle:").trim().toFloatOrDefault(0.33f)
                line.startsWith("data:") ->
                    data = line.removePrefix("data:").trim()
                        .split(Regex("\\s+")).filter { it.isNotEmpty() }
                        .map { it.toIntOrNull() ?: 0 }.toIntArray()
            }
        }
        flush()
        return IrFile(signals)
    }

    // ------------------------------------------------------------------ //
    // Protocol encoders -> int[] of microseconds (mark, space, mark, ...)
    // ------------------------------------------------------------------ //

    private fun encodeParsed(protocol: String, address: String, command: String, freq: Int): IntArray {
        val addrBytes = hexBytes(address)
        val cmdBytes = hexBytes(command)
        return when (protocol.uppercase()) {
            "NEC", "NECEXT" -> encode NecLike(addrBytes, cmdBytes, extended = protocol.uppercase() == "NECEXT")
            "SAMSUNG32" -> encodeSamsung32(addrBytes, cmdBytes)
            "RC5" -> encodeRc5(addrBytes, cmdBytes)
            "RC6" -> encodeRc6(addrBytes, cmdBytes)
            "SIRC", "SIRC12", "SIRC15", "SIRC20" ->
                encodeSirc(addrBytes, cmdBytes, bits = protocol.removePrefix("SIRC").ifEmpty { "12" }.toIntOrDefault(12))
            else -> intArrayOf()   // unknown protocol: caller falls back to raw or skips
        }
    }

    private fun hexBytes(s: String): ByteArray =
        s.split(Regex("\\s+")).filter { it.isNotEmpty() }
            .map { it.toInt(16).toByte() }.toByteArray()

    /** NEC: 9000/4500 header, 32 bits LSB-first, 560us bit cells, trailing mark. */
    private fun encode NecLike(addr: ByteArray, cmd: ByteArray, extended: Boolean): IntArray {
        val a = if (extended && addr.size >= 2) {
            (addr[0].toInt() and 0xFF) or ((addr[1].toInt() and 0xFF) shl 8)
        } else if (addr.isNotEmpty()) addr[0].toInt() and 0xFF else 0
        val c = if (cmd.isNotEmpty()) cmd[0].toInt() and 0xFF else 0
        val bits = mutableListOf<Int>()
        fun pushBit(b: Int) {
            bits += 560
            bits += if (b == 1) 1690 else 560
        }
        bits += 9000; bits += 4500
        val word = if (extended) (a and 0xFFFF) or ((c and 0xFF) shl 16) or (((c.inv()) and 0xFF) shl 24)
                   else a or ((a.inv() and 0xFF) shl 8) or (c shl 16) or (((c.inv()) and 0xFF) shl 24)
        for (i in 0 until 32) pushBit((word shr i) and 1)
        bits += 560
        return bits.toIntArray()
    }

    /** Samsung32: like NEC but 4500/4500? No — header 4500/4500, 32 bits. */
    private fun encodeSamsung32(addr: ByteArray, cmd: ByteArray): IntArray {
        val a = if (addr.isNotEmpty()) addr[0].toInt() and 0xFF else 0
        val c = if (cmd.isNotEmpty()) cmd[0].toInt() and 0xFF else 0
        val bits = mutableListOf(4500, 4500)
        val word = a or (c shl 8) or (((c.inv()) and 0xFF) shl 16) or ((a.inv() and 0xFF) shl 24)
        for (i in 0 until 32) {
            bits += 560
            bits += if ((word shr i) and 1 == 1) 1690 else 560
        }
        bits += 560
        return bits.toIntArray()
    }

    /** RC5: 889us half-bits, Manchester, 14 bits total (2 start + toggle + 5 addr + 6 cmd). */
    private fun encodeRc5(addr: ByteArray, cmd: ByteArray): IntArray {
        val a = if (addr.isNotEmpty()) (addr[0].toInt() and 0x1F) else 0
        val c = if (cmd.isNotEmpty()) (cmd[0].toInt() and 0x3F) else 0
        var toggle = 1
        val bitStream = mutableListOf(1, 1, toggle) +
            (4 downTo 0).map { (a shr it) and 1 } + (5 downTo 0).map { (c shr it) and 1 }
        return manchester(bitStream, 889)
    }

    /** RC6: 444us half-bits, 20 bits (start + mode(3) + toggle + 8 addr + 8 cmd). */
    private fun encodeRc6(addr: ByteArray, cmd: ByteArray): IntArray {
        val a = if (addr.size >= 1) addr[0].toInt() and 0xFF else 0
        val c = if (cmd.size >= 1) cmd[0].toInt() and 0xFF else 0
        val bitStream = mutableListOf(1) + (2 downTo 0).map { 0 } + listOf(0) +
            (7 downTo 0).map { (a shr it) and 1 } + (7 downTo 0).map { (c shr it) and 1 }
        return manchester(bitStream, 444, leadingMark = 2667, leadingSpace = 889)
    }

    private fun manchester(bits: List<Int>, half: Int,
                           leadingMark: Int = 0, leadingSpace: Int = 0): IntArray {
        val out = mutableListOf<Int>()
        if (leadingMark > 0) { out += leadingMark; out += leadingSpace }
        var prevMark = true
        for (b in bits) {
            val first = if (b == 1) (if (prevMark) 0 else half) else (if (prevMark) half else 0)
            val second = half - first
            out += if (first > 0) first else 0
            out += if (second > 0) second else 0
            prevMark = b == 0
        }
        // trim zero-length entries and ensure mark-first
        return out.filter { it > 0 }.toIntArray()
    }

    /** Sony SIRC: 1200us mark = 1, 600us mark = 0; 2400/600 header. LSB-first. */
    private fun encodeSirc(addr: ByteArray, cmd: ByteArray, bits: Int): IntArray {
        val a = if (addr.isNotEmpty()) addr[0].toInt() and 0xFF else 0
        val c = if (cmd.isNotEmpty()) cmd[0].toInt() and 0xFF else 0
        val cmdBits = when (bits) { 12 -> 7; 15 -> 7; else -> 8 }
        val addrBits = bits - 1 - cmdBits
        val stream = (0 until cmdBits).map { (c shr it) and 1 } +
                     (0 until addrBits).map { (a shr it) and 1 }
        val out = mutableListOf(2400, 600)
        for (b in stream) { out += if (b == 1) 1200 else 600; out += 600 }
        return out.toIntArray()
    }

    private fun String.toIntOrDefault(d: Int) = toIntOrNull() ?: d
    private fun String.toFloatOrDefault(d: Float) = toFloatOrNull() ?: d
}
