package com.dceb.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit

enum class Channel(val baseUrl: String) {
    ADB_REVERSE("http://127.0.0.1:8000"),
    LAN("http://192.168.0.100:8000"),      // placeholder, user edits in UI
    TAILSCALE("http://100.64.0.1:8000"),  // placeholder, user edits in UI
}

/**
 * Thin REST client for the PC server. All calls are suspend and IO-bound.
 *
 * Every AI call carries a `session_id` and the recent transcript so Capri keeps
 * short-term memory across turns.
 */
class NetworkClient {

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)   // AI answers can take a while
        .build()

    var channel: Channel = Channel.ADB_REVERSE
    var customBaseUrl: String = ""

    /** Conversation id shared by REST and WS; reset from the AI tab. */
    var sessionId: String = UUID.randomUUID().toString()

    private fun base(): String =
        customBaseUrl.ifBlank { channel.baseUrl }.trimEnd('/')

    suspend fun health(): JSONObject = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$base()/api/v1/health").get().build()
        http.newCall(req).execute().use { r ->
            JSONObject(r.body!!.string())
        }
    }

    /**
     * @param history recent transcript, oldest first (true = user turn).
     */
    suspend fun process(mode: String, rawData: String, deviceContext: Map<String, Any>,
                        history: List<Pair<Boolean, String>> = emptyList()): JSONObject =
        postJson("/api/v1/process", mapOf(
            "mode" to mode, "raw_data" to rawData, "device_context" to deviceContext,
            "session_id" to sessionId, "history" to historyPayload(history)))

    suspend fun parseDump(dumpType: String, rawData: String, deviceContext: Map<String, Any>,
                          history: List<Pair<Boolean, String>> = emptyList()): JSONObject =
        postJson("/api/v1/parse_dump", mapOf(
            "dump_type" to dumpType, "raw_data" to rawData, "device_context" to deviceContext,
            "session_id" to sessionId, "history" to historyPayload(history)))

    /**
     * AUTHORIZED LAN inventory. `confirmOwner` must be true — the operator
     * states the target is theirs. Only ordinary connect() probes are used.
     */
    suspend fun netScan(target: String, ports: List<Int>, confirmOwner: Boolean,
                        allowPublic: Boolean = false, grabBanner: Boolean = false,
                        doPing: Boolean = false, maxHosts: Int = 256): JSONObject =
        postJson("/api/v1/netscan", mapOf(
            "target" to target, "ports" to ports, "confirm_owner" to confirmOwner,
            "allow_public" to allowPublic, "grab_banner" to grabBanner,
            "do_ping" to doPing, "max_hosts" to maxHosts, "session_id" to sessionId))

    /** Drop the server-side transcript and start a fresh conversation. */
    suspend fun resetSession(): JSONObject = withContext(Dispatchers.IO) {
        val body = "{}".toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("$base()/api/v1/session/reset?session_id=$sessionId")
            .post(body).build()
        http.newCall(req).execute().use { r -> JSONObject(r.body!!.string()) }
    }

    suspend fun uploadDump(name: String, content: String): JSONObject = withContext(Dispatchers.IO) {
        val body = content.toRequestBody("text/plain".toMediaType())
        val req = Request.Builder()
            .url("$base()/api/v1/dumps/upload?name=${name.replace(" ", "_")}")
            .post(body).build()
        http.newCall(req).execute().use { JSONObject(it.body!!.string()) }
    }

    suspend fun irdbCategories(): List<String> = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$base()/api/v1/irdb/categories").get().build()
        http.newCall(req).execute().use { r ->
            val arr = JSONObject(r.body!!.string()).getJSONArray("categories")
            List(arr.length()) { arr.getString(it) }
        }
    }

    suspend fun irdbSearch(q: String, category: String? = null): List<Map<String, String>> =
        withContext(Dispatchers.IO) {
            val enc = java.net.URLEncoder.encode(q, "UTF-8")
            val cat = category?.let { "&category=${java.net.URLEncoder.encode(it, "UTF-8")}" } ?: ""
            val req = Request.Builder()
                .url("$base()/api/v1/irdb/search?q=$enc$cat")
                .get().build()
            http.newCall(req).execute().use { r ->
                val arr = JSONObject(r.body!!.string()).getJSONArray("results")
                List(arr.length()) { i ->
                    val o = arr.getJSONObject(i)
                    mapOf("category" to o.getString("category"),
                          "brand" to o.getString("brand"),
                          "model" to o.getString("model"))
                }
            }
        }

    suspend fun irdbFile(category: String, brand: String, model: String): String =
        withContext(Dispatchers.IO) {
            val enc = { s: String -> java.net.URLEncoder.encode(s, "UTF-8") }
            val req = Request.Builder()
                .url("$base()/api/v1/irdb/file?category=${enc(category)}&brand=${enc(brand)}&model=${enc(model)}")
                .get().build()
            http.newCall(req).execute().use { JSONObject(it.body!!.string()).getString("content") }
        }

    private fun historyPayload(history: List<Pair<Boolean, String>>): List<Map<String, String>> =
        history.takeLast(20).map { (fromUser, text) ->
            mapOf("role" to if (fromUser) "user" else "assistant",
                  "content" to text.take(2000))
        }

    private suspend fun postJson(path: String, map: Map<String, Any>): JSONObject =
        withContext(Dispatchers.IO) {
            val json = JSONObject(map)
            val body = json.toString().toRequestBody("application/json".toMediaType())
            val req = Request.Builder().url("$base()$path").post(body).build()
            http.newCall(req).execute().use { r ->
                val text = r.body?.string().orEmpty()
                if (!r.isSuccessful) {
                    throw IllegalStateException(
                        "HTTP ${r.code}: ${text.take(300)}")
                }
                JSONObject(text)
            }
        }

    // --------------------------- WebSocket ------------------------------ //

    @Volatile
    private var ws: okhttp3.WebSocket? = null

    /** True while the streaming WS channel to /ws/v1/ai is connected. */
    @Volatile
    var wsConnected: Boolean = false
        private set

    /**
     * Open the streaming AI channel. onMessage fires per server JSON reply.
     * Falls back silently — callers should keep REST as a backup path.
     */
    fun connectWs(onMessage: (JSONObject) -> Unit, onClose: () -> Unit) {
        val wsUrl = base().replace("http", "ws") + "/ws/v1/ai"
        val listener = object : okhttp3.WebSocketListener() {
            override fun onOpen(w: okhttp3.WebSocket, response: okhttp3.Response) {
                ws = w
                wsConnected = true
            }
            override fun onMessage(w: okhttp3.WebSocket, text: String) {
                runCatching { onMessage(JSONObject(text)) }
            }
            override fun onClosed(w: okhttp3.WebSocket, code: Int, reason: String) {
                wsConnected = false
                onClose()
            }
            override fun onFailure(w: okhttp3.WebSocket, t: Throwable, response: okhttp3.Response?) {
                wsConnected = false
                onClose()
            }
        }
        http.newWebSocket(Request.Builder().url(wsUrl).build(), listener)
    }

    fun wsSend(mode: String, rawData: String, deviceContext: Map<String, Any>,
               history: List<Pair<Boolean, String>> = emptyList()) {
        ws?.send(JSONObject(mapOf(
            "mode" to mode, "raw_data" to rawData, "device_context" to deviceContext,
            "session_id" to sessionId, "history" to historyPayload(history))).toString())
    }

    fun disconnectWs() {
        runCatching { ws?.close(1000, "bye") }
        ws = null
        wsConnected = false
    }
}
