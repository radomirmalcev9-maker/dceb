package com.dceb.core

import android.content.Context
import android.content.SharedPreferences

/**
 * Every knob in one place, persisted in SharedPreferences so the settings
 * survive a restart. Defaults are the "works out of the box" values.
 */
object SettingsRepository {

    private const val PREFS = "dceb_settings"

    private var prefs: SharedPreferences? = null

    fun init(context: Context) {
        prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    }

    private fun p(): SharedPreferences =
        prefs ?: throw IllegalStateException("SettingsRepository.init(context) was not called")

    // ------------------------------------------------------------- Capri --
    var companionEnabled: Boolean
        get() = p().getBoolean("companion_enabled", true)
        set(v) = p().edit().putBoolean("companion_enabled", v).apply()

    /** Frame height in dp. */
    var companionSizeDp: Int
        get() = p().getInt("companion_size_dp", 120)
        set(v) = p().edit().putInt("companion_size_dp", v.coerceIn(48, 320)).apply()

    /** Multiplier for drift + bob speed, 0.25 .. 3.0. */
    var companionSpeed: Float
        get() = p().getFloat("companion_speed", 1.0f)
        set(v) = p().edit().putFloat("companion_speed", v.coerceIn(0.25f, 3.0f)).apply()

    var companionWander: Boolean
        get() = p().getBoolean("companion_wander", true)
        set(v) = p().edit().putBoolean("companion_wander", v).apply()

    var companionBlink: Boolean
        get() = p().getBoolean("companion_blink", true)
        set(v) = p().edit().putBoolean("companion_blink", v).apply()

    var companionOpacity: Float
        get() = p().getFloat("companion_opacity", 1.0f)
        set(v) = p().edit().putFloat("companion_opacity", v.coerceIn(0.2f, 1.0f)).apply()

    /** How long a line stays above her head, seconds. */
    var bubbleSeconds: Int
        get() = p().getInt("bubble_seconds", 6)
        set(v) = p().edit().putInt("bubble_seconds", v.coerceIn(2, 30)).apply()

    /** Idle chatter period, seconds. 0 disables automatic lines. */
    var chatterSeconds: Int
        get() = p().getInt("chatter_seconds", 0)
        set(v) = p().edit().putInt("chatter_seconds", v.coerceIn(0, 3600)).apply()

    /** Look up the foreground app before asking for a line. */
    var collectAppContext: Boolean
        get() = p().getBoolean("collect_app_context", true)
        set(v) = p().edit().putBoolean("collect_app_context", v).apply()

    /** Empty = use the built-in server persona. */
    var persona: String
        get() = p().getString("persona", "") ?: ""
        set(v) = p().edit().putString("persona", v).apply()

    // ---------------------------------------------------------------- AI --
    var historyTurns: Int
        get() = p().getInt("history_turns", 20)
        set(v) = p().edit().putInt("history_turns", v.coerceIn(0, 60)).apply()

    var temperature: Float
        get() = p().getFloat("temperature", 0.1f)
        set(v) = p().edit().putFloat("temperature", v.coerceIn(0.0f, 1.5f)).apply()

    var maxTokens: Int
        get() = p().getInt("max_tokens", 1024)
        set(v) = p().edit().putInt("max_tokens", v.coerceIn(64, 8192)).apply()

    var archiveEnabled: Boolean
        get() = p().getBoolean("archive_enabled", true)
        set(v) = p().edit().putBoolean("archive_enabled", v).apply()

    // ----------------------------------------------------------- network --
    var scanTarget: String
        get() = p().getString("scan_target", "192.168.1.0/24") ?: "192.168.1.0/24"
        set(v) = p().edit().putString("scan_target", v).apply()

    var scanPorts: String
        get() = p().getString("scan_ports", "22,80,443,445,3389,8000,8080,9100")
            ?: "22,80,443,445,3389,8000,8080,9100"
        set(v) = p().edit().putString("scan_ports", v).apply()

    var scanBanners: Boolean
        get() = p().getBoolean("scan_banners", false)
        set(v) = p().edit().putBoolean("scan_banners", v).apply()

    var scanPing: Boolean
        get() = p().getBoolean("scan_ping", false)
        set(v) = p().edit().putBoolean("scan_ping", v).apply()

    /** Public ranges: off by default, and the server asks again anyway. */
    var allowPublic: Boolean
        get() = p().getBoolean("allow_public", false)
        set(v) = p().edit().putBoolean("allow_public", v).apply()

    // -------------------------------------------------------- connection --
    var serverUrl: String
        get() = p().getString("server_url", "") ?: ""
        set(v) = p().edit().putString("server_url", v).apply()

    /** Bump to force every settings screen to re-read the values. */
    var revision: Int
        get() = p().getInt("revision", 0)
        set(v) = p().edit().putInt("revision", v).apply()

    fun bump() {
        revision = revision + 1
    }

    fun reset() {
        p().edit().clear().apply()
        bump()
    }
}
