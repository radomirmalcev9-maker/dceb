package com.dceb.ai

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.dceb.core.HardwareProbe
import com.dceb.core.HardwareStatus
import com.dceb.core.NetworkClient
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject

data class ChatMessage(val fromUser: Boolean, val text: String)

/** One executed/answered server action, kept for repeat & audit. */
data class HistoryEntry(
    val timestamp: Long,
    val action: String,
    val explanation: String,
    val params: JSONObject,
    val outcome: String,          // "approved+executed" | "rejected" | "info"
)

/** A server-suggested action that ALWAYS requires explicit user confirmation. */
data class PendingAction(
    val action: String,
    val params: JSONObject,
    val explanation: String,
)

class AiViewModel(private val net: NetworkClient) : ViewModel() {

    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages

    private val _health = MutableStateFlow<JSONObject?>(null)
    val health: StateFlow<JSONObject?> = _health

    private val _pending = MutableStateFlow<PendingAction?>(null)
    val pending: StateFlow<PendingAction?> = _pending

    private val _hw = MutableStateFlow<HardwareStatus?>(null)
    val hw: StateFlow<HardwareStatus?> = _hw

    /**
     * Every proposed / approved / rejected action, newest first.
     *
     * This is the audit trail the UI shows and the repeat button reads from.
     * It was referenced five times before it existed, which is why the app did
     * not compile.
     */
    private val _history = MutableStateFlow<List<HistoryEntry>>(emptyList())
    val history: StateFlow<List<HistoryEntry>> = _history

    /** Stream connection state, surfaced in the AI tab header. */
    private val _wsLive = MutableStateFlow(false)
    val wsLive: StateFlow<Boolean> = _wsLive

    /** Safety switch: server actions are NEVER executed silently by default. */
    var autoApproveServerActions: Boolean = false

    fun refreshHealth() = viewModelScope.launch {
        runCatching { _health.value = net.health() }
    }

    fun refreshHardware(context: android.content.Context) = viewModelScope.launch {
        _hw.value = HardwareProbe.probe(context.applicationContext)
    }

    /** Streaming WS channel; falls back to REST when not connected. */
    fun connectStreaming() {
        net.connectWs(
            onMessage = { resp ->
                handleServerResponse(resp)
            },
            onClose = { _wsLive.value = false })
    }

    /**
     * Resolve a phrase against the PC's IR database.
     *
     * This is the path that makes Capri trustworthy: the server looks up a
     * stored waveform instead of having a model invent pulse timings. It goes
     * over REST even when the WebSocket is live, because the answer arrives
     * once and needs no streaming.
     */
    fun resolveIr(query: String) = viewModelScope.launch {
        if (query.isBlank()) return@launch
        _messages.value += ChatMessage(true, "[resolve] $query")
        try {
            handleServerResponse(net.resolveIr(query))
        } catch (e: Exception) {
            _messages.value += ChatMessage(false, "resolve error: ${e.message}")
        }
    }

    fun send(mode: String, rawData: String) = viewModelScope.launch {
        _messages.value += ChatMessage(true, "[$mode] ${rawData.take(400)}")
        val ctx = mapOf("hw" to (_hw.value?.summary ?: ""))
        try {
            if (net.wsConnected) {
                net.wsSend(mode, rawData, ctx)   // answer arrives via WS onMessage
            } else {
                handleServerResponse(net.process(mode, rawData, ctx))
            }
        } catch (e: Exception) {
            _messages.value += ChatMessage(false, "error: ${e.message}")
        }
    }

    fun sendDump(dumpType: String, rawData: String) = viewModelScope.launch {
        _messages.value += ChatMessage(true, "[dump:$dumpType] ${rawData.take(400)}")
        try {
            handleServerResponse(net.parseDump(dumpType, rawData, emptyMap()))
        } catch (e: Exception) {
            _messages.value += ChatMessage(false, "error: ${e.message}")
        }
    }

    private fun handleServerResponse(resp: JSONObject) {
        val action = resp.optString("action", "info_only")
        val explanation = resp.optString("explanation", "")
        _messages.value += ChatMessage(false, "$action — $explanation")
        if (action != "info_only") {
            // Server-suggested actions are NEVER executed silently:
            // queued here and require an explicit "Approve" tap in the UI.
            val p = PendingAction(action, resp.optJSONObject("params") ?: JSONObject(), explanation)
            _pending.value = p
            _history.value = listOf(HistoryEntry(System.currentTimeMillis(), action,
                explanation, p.params, "proposed")) + _history.value
        }
    }

    /** Re-run a previously approved action from history (user-initiated repeat). */
    suspend fun repeat(entry: HistoryEntry, context: android.content.Context): String {
        val tmp = _pending.value
        _pending.value = PendingAction(entry.action, entry.params, entry.explanation)
        val result = executePending(context)
        _pending.value = tmp
        _history.value = listOf(entry.copy(timestamp = System.currentTimeMillis(),
                                             outcome = "repeated: $result")) + _history.value.drop(1)
        return result
    }

    /** Called only from the explicit Approve button. Returns a human-readable result. */
    suspend fun executePending(context: android.content.Context): String {
        val p = _pending.value ?: return "nothing pending"
        _pending.value = null
        val result = when (p.action) {
            "transmit_ir" -> {
                val freq = p.params.optInt("frequency", 38000)
                val arr = p.params.optJSONArray("pattern")
                val pattern = IntArray(arr?.length() ?: 0) { arr?.optInt(it) ?: 0 }
                val ok = com.dceb.ir.IrTransmitter.transmit(context, freq, pattern)
                if (ok) "IR sent (${pattern.size} symbols @ ${freq}Hz)" else "IR transmit FAILED"
            }
            "execute_hid" -> {
                val script = p.params.optString("hid_script", "")
                val n = com.dceb.usb.DuckyInterpreter.run(script = script)
                "HID script executed: $n reports written"
            }
            "ui_action" -> {
                // UI automation on THIS device only, still behind the Approve tap.
                if (!com.dceb.core.UiAutomationService.isEnabled())
                    return@executePending "accessibility service not enabled (Settings → Device)"
                val what = p.params.optString("do", "")
                val arg = p.params.optString("arg", "")
                when (what) {
                    "read_screen" -> "screen: " + com.dceb.core.UiAutomationService.readScreen().take(500)
                    "tap_text" -> "tap '$arg': " + com.dceb.core.UiAutomationService.tapText(arg)
                    "tap_xy" -> {
                        val parts = arg.split(",").map { it.trim().toFloatOrNull() ?: 0f }
                        "tap(${parts[0]},${parts[1]}): " + com.dceb.core.UiAutomationService.tap(parts[0], parts[1])
                    }
                    "swipe" -> {
                        val v = arg.split(",").map { it.trim().toFloatOrNull() ?: 0f }
                        "swipe: " + com.dceb.core.UiAutomationService.swipe(v[0], v[1], v[2], v[3])
                    }
                    "open_app" -> "open $arg: " + com.dceb.core.UiAutomationService.openApp(arg)
                    else -> "unknown ui_action: $what"
                }
            }
            // Inventory actions carry their own report in `explanation`; there
            // is nothing to execute on the phone, so show what the PC found.
            "network_scan", "ble_scan" ->
                p.explanation.ifBlank { "scan returned no devices" }
            else -> "no executable action (${p.action})"
        }
        _history.value = listOf(HistoryEntry(System.currentTimeMillis(), p.action,
            p.explanation, p.params, "approved+executed: $result")) + _history.value
        return result
    }

    fun dismissPending() {
        _pending.value?.let { p ->
            _history.value = listOf(HistoryEntry(System.currentTimeMillis(), p.action,
                p.explanation, p.params, "rejected")) + _history.value
        }
        _pending.value = null
    }
}
