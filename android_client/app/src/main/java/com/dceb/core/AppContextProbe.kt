package com.dceb.core

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import java.util.Calendar

/**
 * Collects the little bits of context Capri speaks from: which app the owner is
 * in right now, the hour, the battery. Read-only, and only about this device.
 *
 * The foreground package comes from our own AccessibilityService (the owner
 * turns it on in Settings), never from a hidden tracker or a background poll.
 */
object AppContextProbe {

    data class Snapshot(
        val appPackage: String?,
        val appLabel: String?,
        val hour: Int,
        val batteryPercent: Int?,
        val charging: Boolean,
    )

    fun labelFor(context: Context, pkg: String): String? = try {
        val pm = context.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (e: Exception) {
        null
    }

    fun snapshot(context: Context, withApp: Boolean = true): Snapshot {
        val pkg = if (withApp) UiAutomationService.foregroundApp() else null
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val status = battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        return Snapshot(
            appPackage = pkg,
            appLabel = pkg?.let { labelFor(context, it) },
            hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY),
            batteryPercent = if (level >= 0 && scale > 0) level * 100 / scale else null,
            charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL,
        )
    }

    /** Shape the server prompt expects (see MODE_PROMPTS["companion"]). */
    fun toMap(s: Snapshot): Map<String, Any> = mapOf(
        "app" to (s.appPackage ?: ""),
        "app_label" to (s.appLabel ?: ""),
        "hour" to s.hour,
        "battery" to (s.batteryPercent ?: -1),
        "charging" to s.charging,
    )
}
