package com.dceb.nfc

import android.app.Activity
import android.nfc.NfcAdapter
import android.nfc.Tag
import android.nfc.tech.Ndef
import android.nfc.tech.MifareClassic

/** Reads UID + basic info of presented NFC tags. Works on the owner's own cards. */
object NfcTool {

    data class TagInfo(val uidHex: String, val techList: List<String>, val ndefText: String?)

    fun read(activity: Activity, intent: android.content.Intent): TagInfo? {
        val tag: Tag? = intent.getParcelableExtra(NfcAdapter.EXTRA_TAG)
        if (tag == null) return null
        val uid = tag.id.joinToString("") { "%02X".format(it) }
        val techs = tag.techList.toList()
        var ndefText: String? = null
        runCatching {
            val ndef = Ndef.get(tag)
            if (ndef != null) {
                ndef.connect()
                ndefText = ndef.cachedNdefMessage?.records?.firstOrNull()?.let {
                    String(it.payload).removePrefix("\u0002en").removePrefix("\u0003")
                }
                ndef.close()
            }
        }
        return TagInfo(uid, techs, ndefText)
    }

    fun readMifareClassic(tag: Tag): Map<Int, List<String>> {
        val out = mutableMapOf<Int, List<String>>()
        runCatching {
            val mf = MifareClassic.get(tag) ?: return@runCatching
            mf.connect()
            for (s in 0 until mf.sectorCount) {
                runCatching {
                    if (mf.authenticateSectorWithKeyA(s, MifareClassic.KEY_DEFAULT)) {
                        val blocks = (mf.getBlockCountInSector(s))
                        val data = (0 until blocks).map { b ->
                            mf.readBlock(mf.sectorToBlock(s) + b)
                                .joinToString("") { "%02X".format(it) }
                        }
                        out[s] = data
                    }
                }
            }
            mf.close()
        }
        return out
    }
}
