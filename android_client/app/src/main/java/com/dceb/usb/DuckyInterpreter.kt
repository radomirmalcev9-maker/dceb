package com.dceb.usb

import com.dceb.core.RootShell

/**
 * Minimal DuckyScript -> USB HID interpreter.
 * Converts script lines into raw 8-byte HID keyboard reports written to
 * /dev/hidg0 (boot protocol keyboard).
 *
 * Supported: STRING, DELAY, ENTER, GUI/CTRL/ALT/SHIFT + key combos,
 * single key names (a..z, 0..9, F1..F12, arrows, SPACE, TAB, ESC, etc.)
 *
 * The phone only types into the machine it is physically plugged into —
 * use exclusively on your own or explicitly authorized systems.
 */
object DuckyInterpreter {

    // HID usage IDs (boot keyboard)
    private val KEY = hashMapOf(
        "a" to 0x04, "b" to 0x05, "c" to 0x06, "d" to 0x07, "e" to 0x08, "f" to 0x09,
        "g" to 0x0A, "h" to 0x0B, "i" to 0x0C, "j" to 0x0D, "k" to 0x0E, "l" to 0x0F,
        "m" to 0x10, "n" to 0x11, "o" to 0x12, "p" to 0x13, "q" to 0x14, "r" to 0x15,
        "s" to 0x16, "t" to 0x17, "u" to 0x18, "v" to 0x19, "w" to 0x1A, "x" to 0x1B,
        "y" to 0x1C, "z" to 0x1D,
        "1" to 0x1E, "2" to 0x1F, "3" to 0x20, "4" to 0x21, "5" to 0x22,
        "6" to 0x23, "7" to 0x24, "8" to 0x25, "9" to 0x26, "0" to 0x27,
        "enter" to 0x28, "esc" to 0x29, "backspace" to 0x2A, "tab" to 0x2B,
        "space" to 0x2C, "-" to 0x2D, "=" to 0x2E, "[" to 0x2F, "]" to 0x30,
        "\\" to 0x31, ";" to 0x33, "'" to 0x34, "`" to 0x35, "," to 0x36,
        "." to 0x37, "/" to 0x38, "capslock" to 0x39,
        "f1" to 0x3A, "f2" to 0x3B, "f3" to 0x3C, "f4" to 0x3D, "f5" to 0x3E,
        "f6" to 0x3F, "f7" to 0x40, "f8" to 0x41, "f9" to 0x42, "f10" to 0x43,
        "f11" to 0x44, "f12" to 0x45,
        "printscreen" to 0x46, "scrolllock" to 0x47, "pause" to 0x48,
        "insert" to 0x49, "home" to 0x4A, "pageup" to 0x4B,
        "delete" to 0x4C, "end" to 0x4D, "pagedown" to 0x4E,
        "right" to 0x4F, "left" to 0x50, "down" to 0x51, "up" to 0x52
    )
    private val MOD = hashMapOf(
        "ctrl" to 0x01, "control" to 0x01, "shift" to 0x02, "alt" to 0x04,
        "gui" to 0x08, "windows" to 0x08, "cmd" to 0x08, "meta" to 0x08
    )

    /** Parse a DuckyScript into a list of 8-byte HID reports. */
    fun compile(script: String): List<ByteArray> {
        val reports = mutableListOf<ByteArray>()
        for (raw in script.lineSequence()) {
            val line = raw.trim()
            if (line.isEmpty() || line.startsWith("REM")) continue
            val sp = line.indexOf(' ')
            val cmd = (if (sp < 0) line else line.substring(0, sp)).uppercase()
            val arg = if (sp < 0) "" else line.substring(sp + 1).trim()
            when (cmd) {
                "DELAY" -> reports += listOf<ByteArray>() // delay handled at run time
                "STRING" -> for (ch in arg) {
                    val (mod, key) = charToHid(ch)
                    if (key != 0) reports += report(mod, key)
                    reports += report(0, 0) // release
                }
                "ENTER" -> { reports += report(0, KEY.getValue("enter")); reports += report(0, 0) }
                else -> {
                    val tokens = line.split(Regex("\\s+"))
                    val mods = tokens.drop(1).mapNotNull { MOD[it.lowercase()] }
                        .fold(0) { acc, m -> acc or m }
                    val keyToken = tokens.last().lowercase()
                    val key = KEY[keyToken] ?: continue
                    reports += report(mods, key)
                    reports += report(0, 0)
                }
            }
        }
        return reports
    }

    private fun report(mod: Int, key: Int) =
        byteArrayOf(mod.toByte(), 0, key.toByte(), 0, 0, 0, 0, 0)

    private fun charToHid(c: Char): Pair<Int, Int> = when {
        c in 'a'..'z' -> 0 to KEY.getValue(c.toString())
        c in 'A'..'Z' -> 0x02 to KEY.getValue(c.lowercaseChar().toString())
        c in '1'..'9' -> 0 to KEY.getValue(c.toString())
        c == '0' -> 0 to KEY.getValue("0")
        c == ' ' -> 0 to KEY.getValue("space")
        c == '\n' || c == '\r' -> 0 to KEY.getValue("enter")
        c == '!' -> 0x02 to KEY.getValue("1")
        c == '@' -> 0x02 to KEY.getValue("2")
        c == '#' -> 0x02 to KEY.getValue("3")
        c == '$' -> 0x02 to KEY.getValue("4")
        c == '%' -> 0x02 to KEY.getValue("5")
        c == '^' -> 0x02 to KEY.getValue("6")
        c == '&' -> 0x02 to KEY.getValue("7")
        c == '*' -> 0x02 to KEY.getValue("8")
        c == '(' -> 0x02 to KEY.getValue("9")
        c == ')' -> 0x02 to KEY.getValue("0")
        c == '-' -> 0 to KEY.getValue("-")
        c == '_' -> 0x02 to KEY.getValue("-")
        c == '=' -> 0 to KEY.getValue("=")
        c == '+' -> 0x02 to KEY.getValue("=")
        c == '[' -> 0 to KEY.getValue("[")
        c == '{' -> 0x02 to KEY.getValue("[")
        c == ']' -> 0 to KEY.getValue("]")
        c == '}' -> 0x02 to KEY.getValue("]")
        c == ';' -> 0 to KEY.getValue(";")
        c == ':' -> 0x02 to KEY.getValue(";")
        c == '\'' -> 0 to KEY.getValue("'")
        c == '"' -> 0x02 to KEY.getValue("'")
        c == ',' -> 0 to KEY.getValue(",")
        c == '<' -> 0x02 to KEY.getValue(",")
        c == '.' -> 0 to KEY.getValue(".")
        c == '>' -> 0x02 to KEY.getValue(".")
        c == '/' -> 0 to KEY.getValue("/")
        c == '?' -> 0x02 to KEY.getValue("/")
        c == '\\' -> 0 to KEY.getValue("\\")
        c == '|' -> 0x02 to KEY.getValue("\\")
        c == '`' -> 0 to KEY.getValue("`")
        c == '~' -> 0x02 to KEY.getValue("`")
        else -> 0 to 0
    }

    /**
     * Execute a DuckyScript against /dev/hidg0 with per-line DELAY support.
     * Returns the number of reports written.
     */
    suspend fun run(device: String = "/dev/hidg0", script: String): Int {
        var written = 0
        var pendingDelayMs = 0L
        for (raw in script.lineSequence()) {
            val line = raw.trim()
            if (line.isEmpty() || line.startsWith("REM")) continue
            if (line.uppercase().startsWith("DELAY")) {
                pendingDelayMs = line.substringAfter(' ').trim().toLongOrNull() ?: 0L
                continue
            }
            if (pendingDelayMs > 0) {
                kotlinx.coroutines.delay(pendingDelayMs)
                pendingDelayMs = 0
            }
            for (report in compile(line)) {
                writeReport(device, report)
                written++
            }
        }
        return written
    }

    private suspend fun writeReport(device: String, report: ByteArray) {
        val hex = report.joinToString("") { "%02x".format(it) }
        // Write raw bytes via root: printf with octal escapes is the portable way.
        val octal = report.joinToString("") { "\\%03o".format(it.toInt() and 0xFF) }
        RootShell.exec("sh", "-c", "printf '$octal' > $device")
    }
}
