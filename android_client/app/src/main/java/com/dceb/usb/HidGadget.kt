package com.dceb.usb

import com.dceb.core.RootShell

/**
 * ConfigFS USB gadget control.
 * Enables a HID keyboard/mouse gadget (functions -> /dev/hidg0, /dev/hidg1)
 * plus optional RNDIS for network over USB.
 *
 * NOTE: this configures the phone's OWN USB port. Only plug it into
 * machines you own or are authorized to test.
 */
object HidGadget {

    private const val GADGET = "/config/usb_gadget/g1"

    suspend fun isSupported(): Boolean =
        RootShell.exec("test -d $GADGET && echo yes").stdout.trim() == "yes"

    /** Create/enable a keyboard+mouse gadget. Returns list of /dev/hidgN nodes. */
    suspend fun enableHid(): List<String> {
        val cmds = listOf(
            "cd $GADGET",
            "echo 0x1d6b > idVendor",   // Linux Foundation
            "echo 0x0104 > idProduct",  // multifunction composite gadget
            "mkdir -p functions/hid.0",
            "echo 1 > functions/hid.0/protocol",   // keyboard
            "echo 1 > functions/hid.0/report_desc_len",
            "mkdir -p functions/hid.1",
            "echo 2 > functions/hid.1/protocol",   // mouse
            "mkdir -p configs/c.1",
            "ln -sf functions/hid.0 configs/c.1/",
            "ln -sf functions/hid.1 configs/c.1/",
            "echo 500 > configs/c.1/MaxPower",
            "ls /sys/class/udc | head -n1 > UDC"
        )
        for (c in cmds) RootShell.exec("sh", "-c", c)
        return RootShell.exec("ls /dev/hidg* 2>/dev/null").stdout.lineSequence()
            .map { it.trim() }.filter { it.isNotEmpty() }.toList()
    }

    suspend fun enableRndis(): Boolean {
        val r = RootShell.exec("sh", "-c",
            "cd $GADGET && mkdir -p functions/rndis.0 && " +
            "ln -sf functions/rndis.0 configs/c.1/ 2>/dev/null; echo done")
        return r.ok
    }

    suspend fun disable(): Boolean {
        val r = RootShell.exec("sh", "-c", "echo '' > $GADGET/UDC")
        return r.ok
    }
}
