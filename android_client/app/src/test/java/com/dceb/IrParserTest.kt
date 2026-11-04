package com.dceb

import com.dceb.ir.IrParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class IrParserTest {

    private val sampleParsed = """
        Filetype: IR signals file
        Version: 1
        name: Power
        type: parsed
        protocol: NEC
        address: 04 00
        command: 0B
    """.trimIndent()

    private val sampleRaw = """
        Filetype: IR signals file
        Version: 1
        name: RawTest
        type: raw
        frequency: 38000
        duty_cycle: 0.33
        data: 9000 4500 560 560
    """.trimIndent()

    @Test
    fun parsesParsedEntryToNecPattern() {
        val file = IrParser.parse(sampleParsed)
        assertEquals(1, file.signals.size)
        val s = file.signals[0]
        assertEquals("Power", s.name)
        assertEquals("NEC", s.protocol)
        // NEC: header 9000/4500 + 32 bits + stop mark
        assertEquals(9000, s.pattern[0])
        assertEquals(4500, s.pattern[1])
        assertEquals(2 + 64 + 1, s.pattern.size)
    }

    @Test
    fun parsesRawEntryAsIs() {
        val file = IrParser.parse(sampleRaw)
        assertEquals(1, file.signals.size)
        assertTrue(file.signals[0].pattern.contentEquals(intArrayOf(9000, 4500, 560, 560)))
        assertEquals(38000, file.signals[0].frequency)
    }

    @Test
    fun necBitEncodingLsbFirst() {
        // command 0x01 -> first data bit is 1 -> long space after first mark
        val file = IrParser.parse(sampleParsed.replace("command: 0B", "command: 01"))
        val s = file.signals[0]
        // bits start at index 2: mark 560, space 1690 for bit=1
        assertEquals(560, s.pattern[2])
        assertEquals(1690, s.pattern[3])
    }

    @Test
    fun parsesFlipperAkaiSample() {
        val akai = """
            Filetype: IR signals file
            Version: 1
            name: Power
            type: parsed
            protocol: NECext
            address: 01 72 00 00
            command: 1E E1 00 00
        """.trimIndent()
        val file = IrParser.parse(akai)
        assertEquals("NECext", file.signals[0].protocol)
        assertEquals(67, file.signals[0].pattern.size)
    }
}
