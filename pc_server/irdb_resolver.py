"""Turn a natural-language request into a real IR signal from the local DB.

This is the module that makes Capri actually *do* things instead of inventing
timings. Asking a language model to produce a pulse array is asking it to
hallucinate 70 integers that must land within microseconds; asking it to name
a device is trivial and the database already knows the waveform.

Resolver pipeline for a phrase like ``"epson projector power"`` or
``"включи проектор epson"``:

1. strip button words, leaving the device phrase (``epson projector``)
2. score every ``.ir`` entry on brand / model / category token overlap
3. pick the best entry above a confidence floor
4. inside that file, match a button by EN+RU synonym table, then by fuzzy
   name match
5. return frequency, duty cycle and the stored pattern untouched

Nothing is synthesised. If the resolver is not confident it says so and the
caller falls back to the model — a miss is cheap, a made-up waveform is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, List, Optional

import ircodec
from irdb import IrFile, list_entries, read_entry

# --------------------------------------------------------------------------- #
# Button vocabulary. Russian included because LO talks to Capri in Russian.
# Values are normalised button stems; matching is token-prefix based so
# "volumeup", "volume up", "vol+" and "громче" all land on VOLUME_UP.
# --------------------------------------------------------------------------- #
BUTTON_SYNONYMS: dict[str, set[str]] = {
    "POWER": {"power", "pwr", "on", "off", "включ", "выключ", "питани", "вкл", "выкл"},
    "VOLUME_UP": {"volume up", "vol up", "vol+", "volume+", "louder", "громче",
                  "громче сделай", "прибавь", "добавь звук", "увеличь звук",
                  "увеличь громкость", "плюс звук"},
    "VOLUME_DOWN": {"volume down", "vol down", "vol-", "volume-", "quieter",
                    "тише", "убавь", "уменьши звук", "уменьши громкость",
                    "сбавь звук", "минус звук"},
    "MUTE": {"mute", "silence", "без звука", "звук выкл", "мьют"},
    "CHANNEL_UP": {"channel up", "ch+", "ch up", "next channel", "канал плюс", "следующий канал"},
    "CHANNEL_DOWN": {"channel down", "ch-", "ch down", "prev channel", "канал минус", "предыдущий канал"},
    "PLAY": {"play", "воспроизв", "играть", "плей"},
    "PAUSE": {"pause", "пауз", "стоп кадр"},
    "STOP": {"stop", "стоп", "останов"},
    "NEXT": {"next", "skip", "следующий трек"},
    "PREVIOUS": {"previous", "prev", "предыдущий трек"},
    "UP": {"up", "вверх"},
    "DOWN": {"down", "вниз"},
    "LEFT": {"left", "влево"},
    "RIGHT": {"right", "вправо"},
    "OK": {"ok", "select", "enter", "confirm", "ок", "выбор", "подтверд"},
    "MENU": {"menu", "меню"},
    "HOME": {"home", "главн"},
    "BACK": {"back", "return", "назад"},
    "EXIT": {"exit", "выход"},
    "SOURCE": {"source", "input", "hdmi", "источник", "вход"},
    "INFO": {"info", "инфо"},
    "SETTINGS": {"settings", "setup", "настройк"},
    "BLANK": {"blank", "black screen", "затемн", "чёрный экран"},
    "FREEZE": {"freeze", "замороз"},
    "ZOOM": {"zoom", "зум", "масштаб"},
    "FOCUS": {"focus", "фокус"},
    "KEYSTONE": {"keystone", "трапец"},
    "ASPECT": {"aspect", "формат", "соотношение"},
    "SLEEP": {"sleep", "таймер сна"},
}

# Words that describe the *class* of device. Used to bias the category match
# and to strip a phrase down to "brand + model".
DEVICE_WORDS: dict[str, set[str]] = {
    "Projectors": {"projector", "beamer", "проектор", "проект"},
    "TVs": {"tv", "television", "телевизор", "телик", "тв"},
    "ACs": {"ac", "air conditioner", "conditioner", "кондиционер", "кондей", "сплит"},
    "Fans": {"fan", "вентилятор"},
    "Heaters": {"heater", "обогревател"},
    "Audio_and_Video_Receivers": {"receiver", "ресивер", "amplifier", "усилител"},
    "SoundBars": {"soundbar", "саундбар", "sound bar"},
    "Speakers": {"speaker", "колонк", "динамик"},
    "LED_Lighting": {"led", "light", "strip", "подсветк", "лампа", "светодиод"},
    "Cable_Boxes": {"cable box", "set top box", "приставк"},
    "DVD_Players": {"dvd", "dvd-плеер", "dvd player", "двд", "дивиди", "двд плеер", "дивиди плеер"},
    "Blu-Ray": {"blu ray", "bluray", "блюрей"},
    "Consoles": {"console", "playstation", "xbox", "приставка игровая", "консол", "плойка"},
    "Cameras": {"camera", "камера", "фотоаппарат"},
    "Vacuum_Cleaners": {"vacuum", "пылесос", "робот пылесос"},
    "Bidet": {"bidet", "биде"},
    "Fireplaces": {"fireplace", "камин"},
}

_MIN_BRAND = 3

# LO writes in Russian, but every brand in the database is Latin. Without this
# map "самсунг" and "Samsung" live in different universes.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "ё": "e",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya", "ё": "e",
}


def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(char, char) for char in text)


# Letter-for-letter transliteration is not how brand names actually travel:
# "Sony" in Cyrillic is "Сони", not "Soni"; "Samsung" is "Самсунг", not
# "Samsung" spelled out. These are the ones people type.
BRAND_ALIASES = {
    "сони": "sony", "самсунг": "samsung", "эпсон": "epson", "филипс": "philips",
    "панасоник": "panasonic", "лж": "lg", "элджи": "lg", "шарп": "sharp",
    "тошиба": "toshiba", "хитачи": "hitachi", "митсубиси": "mitsubishi",
    "дживиси": "jvc", "пионер": "pioneer", "техникс": "technics",
    "бенк": "benq", "оптома": "optoma", "виевсоник": "viewsonic",
    "инфокус": "infocus", "канон": "canon", "никон": "nikon",
    "ксерокс": "xerox", "логитеч": "logitech", "дайсон": "dyson",
    "сяоми": "xiaomi", "хуавей": "huawei", "редми": "redmi",
    "деллинг": "dell", "делл": "dell", "леново": "lenovo", "асус": "asus",
    "сяомi": "xiaomi", "бош": "bosch", "сименс": "siemens",
}


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", text.lower().replace("ё", "е")).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t]


def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Resolution:
    """A concrete signal found in the database."""
    category: str
    brand: str
    model: str
    button: str
    frequency: int
    duty_cycle: float
    pattern: List[int]
    confidence: float
    available_buttons: List[str] = field(default_factory=list)

    def params(self) -> dict:
        return {
            "frequency": self.frequency,
            "duty_cycle": self.duty_cycle,
            "pattern": self.pattern,
            "protocol": f"{self.category}/{self.brand}/{self.model}",
            "name": self.button,
        }

    def describe(self) -> str:
        return (f"{self.button} · {self.brand} {self.model} ({self.category}) · "
                f"{len(self.pattern)} symbols @ {self.frequency} Hz")


# --------------------------------------------------------------------------- #
# .ir parsing (server side; the phone has its own Kotlin parser)
# --------------------------------------------------------------------------- #

@dataclass
class ParsedSignal:
    name: str
    frequency: int
    duty_cycle: float
    pattern: List[int]


def parse_ir_file(text: str) -> List[ParsedSignal]:
    """Parse Flipper ``.ir`` text into signals.

    Only ``type: raw`` entries are returned. ``type: parsed`` entries store a
    protocol/address/command triple that must be *encoded* into timings; the
    phone does that in ``IrParser.kt``, and duplicating the encoders here would
    mean two implementations drifting apart. The resolver therefore reports
    such files' button names but cannot hand back a waveform — see
    ``Resolution.pattern`` being empty in that case.
    """
    signals: List[ParsedSignal] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if block.get("name") and block.get("type", "").lower() == "raw":
            raw = block.get("data", "")
            pattern = [int(v) for v in raw.split() if v.lstrip("-").isdigit()]
            if pattern:
                signals.append(ParsedSignal(
                    name=block["name"].strip(),
                    frequency=int(block.get("frequency", "38000") or 38000),
                    duty_cycle=float(block.get("duty_cycle", "0.33") or 0.33),
                    pattern=pattern))
        block.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if "name" in block:
                flush()
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "name" and "name" in block:
            flush()
        block[key] = value.strip()
    if "name" in block:
        flush()
    return signals


def _all_button_names(text: str) -> List[str]:
    return [line.split(":", 1)[1].strip()
            for line in text.splitlines() if line.startswith("name:")]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _device_class_score(query_tokens: set[str], category: str) -> float:
    words = DEVICE_WORDS.get(category)
    if not words:
        return 0.0
    for word in words:
        parts = _tokens(word)
        if parts and all(any(t.startswith(parts[0][:4]) for t in query_tokens) for _ in [0]):
            return 1.0
    return 0.0


def _query_tokens(device_phrase: str) -> set[str]:
    """Tokens for device matching: original, translitterated, brand aliases."""
    tokens = set(device_phrase.split())
    tokens |= set(translit(device_phrase).split())
    for word in device_phrase.split():
        alias = BRAND_ALIASES.get(word)
        if alias:
            tokens.add(alias)
    return tokens


def _entry_score(query_tokens: set[str], entry: IrFile,
                 want_category: Optional[str] = None) -> float:
    """Score one entry against the device part of a query.

    Brand match is the strongest evidence, then the model number, then the
    device class. ``want_category`` comes from an explicit class word in the
    query: "projector" must beat "screen" and "TV" even though all three files
    may mention the same brand.
    """
    score = 0.0
    brand_tokens = _tokens(entry.brand)
    model_tokens = _tokens(entry.model)

    for bt in brand_tokens:
        if len(bt) < _MIN_BRAND:
            continue
        if bt in query_tokens:
            score += 6.0
        elif any(t.startswith(bt) or bt.startswith(t) for t in query_tokens if len(t) >= 3):
            score += 4.0
        elif any(len(t) >= 4 and _fuzzy(t, bt) >= 0.86 for t in query_tokens):
            # 0.86, not 0.82: "make" vs "Makeblock" scores 0.82 and would turn
            # "make me a sandwich" into a projector query.
            score += 2.5

    for mt in model_tokens:
        if len(mt) < 2:
            continue
        if mt in query_tokens:
            score += 3.0
        elif any(t.startswith(mt) or mt.startswith(t) for t in query_tokens if len(t) >= 3):
            score += 1.5

    if want_category:
        # Hard preference, not a nudge: a query that says "projector" should
        # never resolve to a projector *screen* or a TV of the same brand.
        score += 5.0 if entry.category == want_category else -3.0
    elif _device_class_score(query_tokens, entry.category) > 0:
        score += 2.0
    elif any(t.startswith(_norm(entry.category)[:4]) for t in query_tokens if len(t) >= 4):
        score += 1.0

    return score


def _category_for(query_tokens: set[str]) -> Optional[str]:
    """Explicit device class named in the query, if any."""
    for category, words in DEVICE_WORDS.items():
        for word in words:
            parts = _tokens(word)
            if parts and any(t.startswith(parts[0][:4]) for t in query_tokens):
                return category
    return None


# Signal names in the wild are wildly inconsistent: Vol_up, VOL+, Vol_dn,
# Volume_Down, VOL-. Matching on the literal string is hopeless, so every name
# is reduced to a canonical concept first, then compared concept to concept.
# Order matters: multi-word concepts are tested before single-word ones, or
# "volume up" would be swallowed by "up".
_NAME_CONCEPTS: list[tuple[str, tuple[str, ...]]] = [
    ("VOLUME_UP", ("volup", "volumeup", "volplus", "volumeplus", "volp")),
    ("VOLUME_DOWN", ("voldn", "voldwn", "voldown", "volumedown",
                     "volminus", "volumeminus", "volm")),
    ("CHANNEL_UP", ("chup", "channelup", "chplus", "channelplus")),
    ("CHANNEL_DOWN", ("chdn", "chdwn", "chdown", "channeldown", "chminus")),
    ("MUTE", ("mute", "muting", "mutebtn")),
    ("POWER", ("power", "pwr", "powertoggle", "poweronoff")),
    ("OK", ("ok", "select", "enter", "confirm", "centre", "center")),
    ("UP", ("up", "arrowup", "cursorup")),
    ("DOWN", ("down", "arrowdown", "cursordown")),
    ("LEFT", ("left", "arrowleft", "cursorleft")),
    ("RIGHT", ("right", "arrowright", "cursorright")),
    ("MENU", ("menu", "mainmenu")),
    ("SOURCE", ("source", "input", "sourceselect")),
    ("PLAY", ("play", "playpause")),
    ("PAUSE", ("pause",)),
    ("STOP", ("stop",)),
]


def _name_concept(name: str) -> Optional[str]:
    """Canonical concept for a signal name, e.g. ``Vol_dn`` -> VOLUME_DOWN.

    Punctuation carries meaning in this database: ``VOL+`` and ``VOL-`` are the
    most common spellings of volume up/down (125 and 119 files), so ``+`` and
    ``-`` are mapped to ``plus``/``minus`` instead of being stripped.
    """
    text = name.lower()
    text = text.replace("+", " plus ").replace("-", " minus ")
    squashed = re.sub(r"[^0-9a-zа-я]+", "", _norm(text))
    for concept, markers in _NAME_CONCEPTS:
        for marker in markers:
            if squashed in (marker,) or squashed.startswith(marker):
                return concept
    for concept, markers in _NAME_CONCEPTS:
        for marker in markers:
            if marker in squashed:
                return concept
    return None


def _query_concepts(query: str) -> list[str]:
    """Concepts the user asked for, most specific first.

    The tricky part is that concepts nest: "volume up" contains "up", and
    "next channel" contains "channel up". A naive pass returns
    ``[VOLUME_DOWN, VOLUME_UP, UP]`` for "volume up" because the substring
    "vol" also appears in "volume down"s synonym list. So every synonym hit is
    collected with its span, longer spans claim their characters first, and
    later hits that overlap an already-claimed span are dropped.
    """
    norm_query = _norm(query)
    hits: list[tuple[int, int, str]] = []      # (start, end, canonical)
    for canonical, synonyms in BUTTON_SYNONYMS.items():
        for phrase in synonyms:
            phrase_norm = _norm(phrase)
            if not phrase_norm:
                continue
            # Russian is heavily inflected: the table stores stems
            # ("включ", "громче") so a plain boundary check on the *end* would
            # never match "включи" or "громкость". Anchor the start only, and
            # keep the end anchor for Latin terms where prefixes lie
            # ("up" inside "update").
            # Latin terms need the end anchor (no "up" inside "update"); Cyrillic
            # stems must run free to the end of the word, because the table
            # stores "пауза"/"включ" and the sentence says "паузу"/"включи".
            end_anchor = r"(?![0-9a-z])" if phrase_norm.isascii() else ""
            pattern = rf"(?<![0-9a-zа-я]){re.escape(phrase_norm)}{end_anchor}"
            for match in re.finditer(pattern, norm_query):
                hits.append((match.start(), match.end(), canonical))

    # Longest span first; ties go to whichever appears earlier in the query.
    hits.sort(key=lambda hit: (-(hit[1] - hit[0]), hit[0]))
    claimed = [False] * len(norm_query)
    survivors: list[tuple[int, str]] = []
    for start, end, canonical in hits:
        if any(claimed[start:end]):
            continue
        for index in range(start, end):
            claimed[index] = True
        survivors.append((start, canonical))
    survivors.sort(key=lambda pair: pair[0])

    ordered: list[str] = []
    for _, canonical in survivors:
        if canonical not in ordered:
            ordered.append(canonical)
    return ordered


def _match_button(query: str, names: Iterable[str]) -> Optional[str]:
    """Pick the signal name that best matches the button words in ``query``."""
    names = list(names)
    if not names:
        return None
    wanted = _query_concepts(query)

    # 1) concept-to-concept: the most specific concept the query asked for
    if wanted:
        by_concept: dict[str, list[str]] = {}
        for name in names:
            concept = _name_concept(name)
            if concept:
                by_concept.setdefault(concept, []).append(name)
        for concept in wanted:
            if concept in by_concept:
                return by_concept[concept][0]
        # A suffix in the query ("vol_up" for a file whose button is
        # "Volume_Up") also lands here because concepts are normalised.

    # 2) the query literally names the button
    norm_query = _norm(query)
    for name in names:
        name_norm = _norm(name)
        if name_norm and re.search(
                rf"(?<![0-9a-z]){re.escape(name_norm)}(?![0-9a-z])", norm_query):
            return name

    # 3) fuzzy on the whole string
    best_name, best_ratio = None, 0.0
    for name in names:
        ratio = _fuzzy(_norm(name), norm_query)
        if ratio > best_ratio:
            best_name, best_ratio = name, ratio
    if best_ratio >= 0.72:
        return best_name
    return None


def _strip_button_words(query: str) -> str:
    """Remove button vocabulary, leaving the device phrase."""
    out = _norm(query)
    for synonyms in BUTTON_SYNONYMS.values():
        for phrase in synonyms:
            p = _norm(phrase)
            if p:
                out = re.sub(rf"\b{re.escape(p)}\w*", " ", out)
    for filler in ("please", "can you", "the", "a", "my", "пожалуйста", "мне", "на", "у"):
        out = re.sub(rf"\b{filler}\b", " ", out)
    return re.sub(r"\s+", " ", out).strip()


CONFIDENCE_FLOOR = 6.0


def _parsed_entries(text: str) -> list[dict]:
    """Split a .ir file into raw dicts covering both entry types."""
    entries: list[dict] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("name"):
            entries.append(dict(current))
        current.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if "name" in current:
                flush()
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "name" and "name" in current:
            flush()
        current[key] = value.strip()
    if "name" in current:
        flush()
    return entries


def _waveform_for(entry: dict, fallback_freq: int = 38000) -> tuple[int, float, list[int]]:
    """(frequency, duty_cycle, pattern) for one .ir entry, encoding if needed."""
    kind = (entry.get("type") or "").strip().lower()
    if kind == "raw":
        try:
            frequency = int(entry.get("frequency", fallback_freq) or fallback_freq)
        except ValueError:
            frequency = fallback_freq
        try:
            duty = float(entry.get("duty_cycle", "0.33") or 0.33)
        except ValueError:
            duty = 0.33
        pattern = [int(v) for v in (entry.get("data") or "").split()
                   if v.lstrip("-").isdigit()]
        return frequency, duty, pattern

    # type: parsed -> encode protocol/address/command into timings
    protocol = entry.get("protocol", "")
    frequency = 38000
    if protocol.upper().startswith("SIRC"):
        frequency = 40000
    elif protocol.upper() in ("RC5", "RC6", "RC5X"):
        frequency = 36000
    pattern = ircodec.encode(protocol, entry.get("address", ""),
                             entry.get("command", ""), frequency)
    return frequency, 0.33, pattern


def resolve(query: str, limit: int = 400) -> Optional[Resolution]:
    """Resolve ``query`` to a transmittable IR signal, or ``None`` when unsure."""
    device_phrase = _strip_button_words(query)
    if not device_phrase:
        return None
    # Russian brand names arrive in Cyrillic; the database is Latin-only.
    device_tokens = _query_tokens(device_phrase)
    want_category = _category_for(device_tokens)

    scored = []
    for entry in list_entries():
        score = _entry_score(device_tokens, entry, want_category=want_category)
        if score > 0:
            scored.append((score, entry))
    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], len(pair[1].model)))
    top_score, top_entry = scored[0]
    if top_score < CONFIDENCE_FLOOR:
        return None

    text = read_entry(top_entry.category, top_entry.brand, top_entry.model)
    if text is None:
        return None

    entries = _parsed_entries(text)
    names = [e["name"] for e in entries]
    button = _match_button(query, names)

    # Transmit exactly what was asked for. Falling back to "the first signal in
    # the file" is how a "pause" request ends up opening the disc tray, so an
    # unmatched button returns a Resolution with an empty pattern and the
    # button list — the caller turns that into "which button?".
    candidates: list[dict] = [entry for entry in entries if entry["name"] == button]

    for candidate in candidates:
        frequency, duty, pattern = _waveform_for(candidate)
        if pattern:
            return Resolution(
                category=top_entry.category, brand=top_entry.brand,
                model=top_entry.model, button=_display_button(candidate["name"]),
                frequency=frequency, duty_cycle=duty, pattern=pattern,
                confidence=min(0.95, 0.5 + top_score / 40.0),
                available_buttons=names[:40])

    # Device found, but no entry in the file could be turned into timings.
    return Resolution(
        category=top_entry.category, brand=top_entry.brand, model=top_entry.model,
        button=_display_button(button) if button else "",
        frequency=38000, duty_cycle=0.33, pattern=[],
        confidence=min(0.6, 0.3 + top_score / 60.0),
        available_buttons=names[:40])


def _display_button(name: str) -> str:
    """Tidy a shouty signal name for the UI: "POWER" -> "Power".

    Only ALL-CAPS names are rewritten — Flipper files shout, the projector
    database does not. Mixed-case names like "Volume_Up" are left exactly as
    the file spells them, because that is what a follow-up query must match.
    """
    if not name or not name.isupper():
        return name
    spaced = name.replace("_", " ").replace("-", " ").strip().title()
    return " ".join(spaced.split())


def suggestions(query: str, limit: int = 5) -> List[str]:
    """Entry labels near ``query``, for a helpful "did you mean" reply."""
    device_phrase = _strip_button_words(query)
    if not device_phrase:
        return []
    device_tokens = _query_tokens(device_phrase)
    want_category = _category_for(device_tokens)
    scored = sorted(((s, e) for e in list_entries()
                     if (s := _entry_score(device_tokens, e, want_category)) > 0),
                    key=lambda pair: -pair[0])
    return [f"{e.category}/{e.brand}/{e.model}" for _, e in scored[:limit]]
