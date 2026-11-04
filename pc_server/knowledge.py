"""Tiny local knowledge base used to ground the Capricorn prompt.

This is a deliberately small, offline, dependency-free "RAG-lite" layer:
``retrieve(query)`` scores the notes below by keyword overlap and returns the
top-k snippets, which the server injects into the system prompt.

Scope note: every note is written for AUTHORIZED work only — the owner's own
devices, an owned lab range, or a CTF/practice box. Nothing here describes
stealth, evasion, propagation or data exfiltration, and the retrieval layer
is only ever used to produce analysis/advice, never to execute anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "how", "why",
    "you", "your", "are", "was", "were", "can", "could", "would", "should",
    "into", "out", "get", "got", "has", "have", "had", "not", "all", "any",
    "but", "its", "it's", "my", "me", "on", "in", "of", "to", "a", "an", "is",
    "do", "does", "did", "please", "help", "need", "want", "about", "there",
    "here", "when", "who", "whom", "which", "than", "then", "them", "they",
}


@dataclass(frozen=True)
class Note:
    topic: str
    keywords: tuple
    text: str


NOTES: List[Note] = [
    Note(
        topic="ir-nec",
        keywords=("nec", "ir", "infrared", "remote", "timing", "protocol", "burst"),
        text=(
            "NEC infrared protocol: 38 kHz carrier (typically 33% duty), a lead "
            "pulse of ~9000 us mark followed by ~4500 us space, then 32 bits "
            "(address, inverted address, command, inverted command). A logical 0 "
            "is ~560 us mark + ~560 us space; a logical 1 is ~560 us mark + ~1690 us "
            "space. A repeat frame is ~9000 us mark, ~2250 us space, ~560 us mark, "
            "repeating every ~108 ms while a button is held."
        ),
    ),
    Note(
        topic="ir-protocols",
        keywords=("rc5", "rc6", "sirc", "samsung", "necx", "nec-ext", "protocol"),
        text=(
            "Other common IR protocols: RC5 (36 kHz, 14-bit Manchester/bi-phase, "
            "889 us half-bit), RC6 (36 kHz, variable, leader + start bit), SIRC/Sony "
            "(40 kHz, 12/15/20-bit, 2400 us mark lead, 600 us space = 0, 1200 us = 1), "
            "Samsung32 (38 kHz, NEC-like lead, 32-bit word). Flipper `.ir` files may "
            "store parsed protocol/address/command or raw `data[]` microsecond arrays."
        ),
    ),
    Note(
        topic="ir-flipper-format",
        keywords=("flipper", "ir file", ".ir", "duty_cycle", "frequency", "raw"),
        text=(
            "Flipper IR file format: header `Filetype: IR signals file`, `Version: 1`, "
            "then repeated records. Parsed records carry `name`, `type: parsed`, "
            "`protocol`, `address`, `command`. Raw records carry `name`, `type: raw`, "
            "`frequency` (Hz), `duty_cycle` (0..1) and `data` as an alternating "
            "mark/space microsecond list. Lines starting with `#` are comments."
        ),
    ),
    Note(
        topic="ir-hardware",
        keywords=("consumerir", "transmit", "blaster", "emit", "carrier"),
        text=(
            "Android IR: `ConsumerIrManager.hasIrEmitter()` then "
            "`transmit(carrierFrequency, pattern)` where pattern is alternating "
            "on/off in microseconds. Root fallback: "
            "`cmd consumer_ir transmit <freq> <pattern>`. Typical carrier is 36-40 kHz; "
            "38 kHz covers the majority of consumer gear."
        ),
    ),
    Note(
        topic="nfc-basics",
        keywords=("nfc", "tag", "uid", "ntag", "ndef", "atqa", "sak", "mifare"),
        text=(
            "NFC tags: NTAG213/215/216 store NDEF messages; a read returns a 7-byte "
            "UID, ATQA and SAK bytes that identify the chip family, plus optional NDEF "
            "records (URI, text, MIME). MIFARE Classic 1K uses 16 sectors with two "
            "6-byte keys per sector; many deployments keep factory default keys, but "
            "testing keys against a tag you do not own is unauthorized — only handle "
            "your own tags."
        ),
    ),
    Note(
        topic="subghz-basics",
        keywords=("subghz", "sub-ghz", "433", "868", "315", "ook", "ask", "rolling"),
        text=(
            "Sub-GHz remotes commonly use 315/433.92/868 MHz with OOK or ASK "
            "modulation. Static-code remotes replay fine from a captured raw timing "
            "dump. Rolling-code systems (Keeloq and similar) transmit a new counter "
            "with every press, so a naive replay is refused by the receiver — captured "
            "frames are still useful for identifying the device family. Stay inside "
            "your region's legal ISM band and power limits."
        ),
    ),
    Note(
        topic="bluetooth-inventory",
        keywords=("bluetooth", "ble", "bt", "advertising", "rssi", "beacon", "mac"),
        text=(
            "Bluetooth inventory: classic discovery returns name, MAC and class of "
            "device; BLE scanning returns advertising packets on channels 37/38/39 "
            "with AD types such as 0x09 (complete local name), 0x03 (16-bit service "
            "UUID) and 0xFF (manufacturer data). RSSI in dBm gives a rough proximity "
            "hint only (about -40 near, -70 mid, -90 at the edge). Major device "
            "classes: 0x01 computer, 0x02 phone, 0x04 audio/video, 0x05 peripheral, "
            "0x06 imaging. Inventory is passive observation — pairing, connecting or "
            "reading device data requires the owner's consent."
        ),
    ),
    Note(
        topic="network-ranges",
        keywords=("rfc1918", "private", "subnet", "lan", "cidr", "192.168", "10.", "172"),
        text=(
            "Private IPv4 ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, plus "
            "loopback 127.0.0.0/8 and link-local 169.254.0.0/16. A /24 holds 256 "
            "addresses (254 usable). Anything outside these is public address space "
            "and must only be scanned with written authorization."
        ),
    ),
    Note(
        topic="network-ports",
        keywords=("port", "ports", "22", "80", "443", "445", "3389", "banner", "service"),
        text=(
            "Common ports: 22 SSH, 23 Telnet (insecure), 80 HTTP, 443 HTTPS, 445 SMB, "
            "139 NetBIOS, 3389 RDP, 5900 VNC, 8000/8080/8888 alternate HTTP, 9100 "
            "printer jetdirect, 5432 Postgres, 3306 MySQL. A banner is the greeting a "
            "service prints on connect; grabbing it on an authorized host is ordinary "
            "asset inventory."
        ),
    ),
    Note(
        topic="network-scan-method",
        keywords=("scan", "nmap", "socket", "connect", "sweep", "inventory", "discover"),
        text=(
            "An authorized inventory scan uses a normal TCP connect() per port "
            "(a full handshake, nothing stealthy) plus an optional ICMP ping to find "
            "live hosts first. Keep the port list small, set a sub-second timeout, "
            "cap concurrency, and record what you scanned and when. This tool never "
            "does FIN/XMAS/NULL scans, fragmentation, decoys, timing evasion or "
            "exploitation."
        ),
    ),
    Note(
        topic="hardening-ssh",
        keywords=("ssh", "harden", "hardenin", "root login", "key", "mfa", "fail2ban"),
        text=(
            "SSH hardening: key-based auth only (`PasswordAuthentication no`), "
            "`PermitRootLogin no`, restrict `AllowUsers`, enable rate limiting or "
            "fail2ban, disable weak ciphers/MACs, keep the port firewalled to your "
            "own management network, and enable MFA for interactive logins."
        ),
    ),
    Note(
        topic="hardening-smb-rdp",
        keywords=("smb", "rdp", "nla", "shares", "signing", "windows", "vnc"),
        text=(
            "SMB/RDP hardening: disable SMBv1, require SMB signing where supported, "
            "audit shares for world-readable data, enable NLA for RDP, put RDP behind "
            "a VPN or firewall, enforce lockout policy and MFA, and expose none of it "
            "directly to the internet."
        ),
    ),
    Note(
        topic="hardening-general",
        keywords=("harden", "checklist", "update", "patch", "firewall", "logging", "baseline"),
        text=(
            "General hardening baseline: patch OS and firmware, remove unused "
            "services, default-deny inbound firewall, unique credentials plus a "
            "password manager, MFA everywhere it is offered, full-disk encryption, "
            "centralized logging with retention, and documented, tested backups."
        ),
    ),
    Note(
        topic="owasp-top10",
        keywords=("owasp", "web", "injection", "xss", "ssrf", "access control", "top10"),
        text=(
            "OWASP Top 10 (2021): Broken Access Control, Cryptographic Failures, "
            "Injection, Insecure Design, Security Misconfiguration, Vulnerable and "
            "Outdated Components, Identification and Authentication Failures, Software "
            "and Data Integrity Failures, Security Logging and Monitoring Failures, "
            "Server-Side Request Forgery."
        ),
    ),
    Note(
        topic="ctf-encodings",
        keywords=("ctf", "base64", "hex", "rot13", "caesar", "xor", "morse", "binary", "decode"),
        text=(
            "CTF decoding quick wins: base64 (A-Za-z0-9+/=, decode in 4-char blocks), "
            "hex pairs, ROT13 (alphabet shifted 13), Caesar (try all 25 shifts), "
            "single-byte XOR (look for a key that yields printable ASCII), Morse "
            "(dots/dashes), binary (8-bit groups → ASCII). Look for padded `=` "
            "trailers and repeated short keys."
        ),
    ),
    Note(
        topic="ctf-method",
        keywords=("ctf", "challenge", "flag", "practice", "writeup", "lab"),
        text=(
            "CTF method: identify the category (crypto, forensics, pwn, web, recon, "
            "stego, reversing), extract strings/metadata first, check encodings before "
            "assuming encryption, keep notes per hypothesis, and solve on platforms "
            "that explicitly authorize the target. Never point tooling at a host you "
            "have not been given permission to test."
        ),
    ),
    Note(
        topic="logs-dmesg",
        keywords=("dmesg", "kernel", "log", "segfault", "oom", "usb", "wlan", "error"),
        text=(
            "Kernel log triage: search for `segfault`, `OOM`/`killed process`, "
            "`I/O error`, `reset`/`disconnect` on USB, `wlan` association changes, "
            "thermal throttling and `call trace`. Group repeated lines, note the "
            "timestamp deltas, and correlate a crash with the device or driver named "
            "in the preceding lines."
        ),
    ),
    Note(
        topic="logs-android",
        keywords=("logcat", "android", "console", "crash", "anr", "tag"),
        text=(
            "Android logcat triage: filter by tag and priority, look for "
            "`FATAL EXCEPTION`, `ANR`, `ActivityManager` kills and `audit`/`avc: "
            "denied` SELinux lines, which usually indicate a permission problem "
            "rather than a bug. Pair a crash with the last few lines before it."
        ),
    ),
    Note(
        topic="detection-rules",
        keywords=("sigma", "yara", "detection", "rule", "ioc", "alert"),
        text=(
            "Sigma is a vendor-agnostic log detection format (title, logsource, "
            "detection with selection/condition, level). YARA describes file or "
            "memory patterns (strings with $name, condition with `all of them`). "
            "Both are for defending your own estate and for lab/CTF work."
        ),
    ),
    Note(
        topic="hid-ducky",
        keywords=("hid", "ducky", "duckyscript", "keystroke", "gadget", "configfs"),
        text=(
            "DuckyScript basics: `REM` comment, `DELAY ms`, `STRING text`, `ENTER`, "
            "`GUI r`, `CTRL ALT DELETE`. The ConfigFS USB gadget exposes "
            "`/dev/hidg0`; each HID report is an 8-byte keyboard packet. Typing into "
            "a machine you do not own is unauthorized — authorized lab hosts only."
        ),
    ),
    Note(
        topic="scope-rules",
        keywords=("scope", "authorized", "permission", "legal", "consent", "rules", "policy"),
        text=(
            "Authorization rules for this assistant: act only against devices and "
            "networks the owner controls or has written permission to test; never "
            "attempt to stay hidden, evade logging, disable defenses, move laterally "
            "beyond the stated scope, or copy data off a device without the owner's "
            "say-so. Prefer read-only inventory, and always say what is uncertain."
        ),
    ),
]


def tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#._-]*", text.lower())
            if len(w) > 2 and w not in STOPWORDS]


def retrieve(query: str, k: int = 3) -> List[Note]:
    """Score notes by keyword overlap and return the best ``k``."""
    tokens = set(tokenize(query))
    if not tokens:
        # No usable tokens: fall back to the scope note so safety text is present.
        return [n for n in NOTES if n.topic == "scope-rules"][:k]

    scored = []
    for note in NOTES:
        score = 0.0
        for kw in note.keywords:
            kw_tokens = set(tokenize(kw))
            if kw_tokens and kw_tokens <= tokens:
                score += 2.0 + len(kw_tokens)
            elif kw in query.lower():
                score += 1.5
        score += len(tokens & set(tokenize(note.text))) * 0.15
        scored.append((score, note))
    scored.sort(key=lambda pair: (-pair[0], pair[1].topic))
    return [note for score, note in scored if score > 0][:k]


def render(notes: List[Note]) -> str:
    """Render notes as a compact context block for the prompt."""
    if not notes:
        return ""
    return "\n".join(f"- [{n.topic}] {n.text}" for n in notes)
