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
                val action = resp.optString("action", "info_only")
                _messages.value += ChatMessage(false, "$action — ${resp.optString("explanation", "")}")
                if (action != "info_only") {
                    val p = PendingAction(action, resp.optJSONObject("params") ?: JSONObject(),
                                          resp.optString("explanation", ""))
                    _pending.value = p
                    _history.value = listOf(HistoryEntry(System.currentTimeMillis(), action,
                        p.explanation, p.params, "proposed")) + _history.value
                }
            },
            onClose = { })
    }

    /** Recent transcript as (isUser, text) pairs, oldest first. */
    private fun historyPayload(): List<Pair<Boolean, String>> =
        _messages.value.takeLast(20).map { it.fromUser to it.text }

    fun send(mode: String, rawData: String) = viewModelScope.launch {
        val hist = historyPayload()          // prior turns, before this one
        _messages.value += ChatMessage(true, "[$mode] ${rawData.take(400)}")
        val ctx = mapOf("hw" to (_hw.value?.summary ?: ""))
        try {
            if (net.wsConnected) {
                net.wsSend(mode, rawData, ctx, hist)  // answer via WS onMessage
            } else {
                handleServerResponse(net.process(mode, rawData, ctx, hist))
            }
        } catch (e: Exception) {
            _messages.value += ChatMessage(false, "error: ${e.message}")
        }
    }

    fun sendDump(dumpType: String, rawData: String) = viewModelScope.launch {
        val hist = historyPayload()
        _messages.value += ChatMessage(true, "[dump:$dumpType] ${rawData.take(400)}")
        try {
            handleServerResponse(net.parseDump(dumpType, rawData, emptyMap(), hist))
        } catch (e: Exception) {
            _messages.value += ChatMessage(false, "error: ${e.message}")
        }
    }

    /** Send a passive Bluetooth inventory for analysis (advisory only). */
    fun sendBluetoothInventory(devices: List<com.dceb.core.BtDevice>) {
        send("bluetooth", com.dceb.core.BtInventory.toDump(devices))
    }

    /**
     * AUTHORIZED LAN inventory. `confirmOwner` must be true — only networks the
     * owner controls. Returns a human-readable summary.
     */
    suspend fun netScan(target: String, ports: List<Int>, confirmOwner: Boolean,
                        allowPublic: Boolean = false): String {
        if (!confirmOwner) return "tick 'my network' first — authorized scope only"
        return try {
            val r = net.netScan(target, ports, confirmOwner, allowPublic)
            val results = r.optJSONArray("results") ?: return "no results"
            val lines = mutableListOf<String>()
            for (i in 0 until results.length()) {
                val h = results.getJSONObject(i)
                val open = h.optJSONArray("open_ports")
                val openPorts = (0 until (open?.length() ?: 0)).mapNotNull { j ->
                    open?.optInt(j)?.takeIf { it > 0 } }
                if (openPorts.isNotEmpty()) lines += "${h.optString("host")}: ${openPorts.joinToString()}"
            }
            val advice = r.optJSONObject("advice")?.optString("explanation").orEmpty()
            (if (lines.isEmpty()) listOf("no open ports found")
             else lines).joinToString("
") + if (advice.isNotBlank()) "

$advice" else ""
        } catch (e: Exception) {
            "scan failed: ${e.message}"
        }
    }

    /** Forget the conversation on both the phone and the server. */
    fun resetSession() = viewModelScope.launch {
        runCatching { net.resetSession() }
        _messages.value = emptyList()
        _history.value = emptyList()
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
