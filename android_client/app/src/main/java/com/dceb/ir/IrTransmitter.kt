package com.dceb.ir

import android.content.Context
import android.hardware.ConsumerIrManager
import com.dceb.core.RootShell

/**
 * Sends IR signals via the built-in blaster.
 * Primary path: ConsumerIrManager.transmit().
 * Fallback path: `cmd consumer_ir transmit <freq> <pattern>` through root.
 */
object IrTransmitter {

    private fun consumerIr(context: Context): ConsumerIrManager? =
        context.getSystemService(Context.CONSUMER_IR_SERVICE) as? ConsumerIrManager

    suspend fun transmit(context: Context, frequency: Int, pattern: IntArray): Boolean {
        // 1) Native API
        val cir = consumerIr(context)
        if (cir?.hasIrEmitter() == true) {
            runCatching {
                cir.transmit(frequency, pattern)
                return true
            }
        }
        // 2) Console fallback via root
        if (RootShell.isRootAvailable) {
            val args = pattern.joinToString(" ")
            val r = RootShell.exec("cmd", "consumer_ir", "transmit", frequency.toString(), args)
            if (r.ok) return true
        }
        return false
    }

    suspend fun transmitSignal(context: Context, signal: IrParser.IrSignal): Boolean =
        transmit(context, signal.frequency, signal.pattern)
}
