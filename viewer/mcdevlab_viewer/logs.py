"""Minecraft/NeoForge log handling: normalise, group, diff and crash signature.

Input logs come in three shapes, sometimes mixed in one file (the CT224 `client.log` starts as log4j XML
events and continues as plain text):
  * log4j XML events   <log4j:Event logger= timestamp= level= thread=> <log4j:Message><![CDATA[..]]>
  * plain              [06:55:36] [Render thread/ERROR] [mojang/Yggdrasil]: message
  * plain with date    [05Oct2026 00:01:06.533] [Render thread/INFO] [logger/]: message
"""
from __future__ import annotations

import datetime as _dt
import html
import re
from collections import OrderedDict

# ----------------------------------------------------------------------------------------------
# line reading
# ----------------------------------------------------------------------------------------------

_XML_START = re.compile(r'^\s*<log4j:Event\b(?P<attrs>[^>]*)>\s*$')
_XML_ATTR = re.compile(r'(\w+)="([^"]*)"')
_XML_MSG_INLINE = re.compile(r"<log4j:Message>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</log4j:Message>", re.S)

LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")
_HEADER = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[(?P<thread>[^\]]*)/(?P<level>[A-Z]+)\](?:\s+\[(?P<logger>[^\]]*)\])?:\s?(?P<msg>.*)$")


def _fmt_ts(ms):
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000.0, _dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    except (ValueError, OSError, OverflowError):
        return "00:00:00.000"


def to_lines(text: str):
    """Return plain-text log lines; XML events are rendered in the plain format."""
    out = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        m = _XML_START.match(ln)
        if not m:
            out.append(ln)
            i += 1
            continue
        attrs = dict(_XML_ATTR.findall(m.group("attrs")))
        j = i + 1
        block = []
        while j < n and "</log4j:Event>" not in lines[j]:
            block.append(lines[j])
            j += 1
        raw = "\n".join(block)
        mm = _XML_MSG_INLINE.search(raw)
        if mm:
            msg = mm.group(1) if mm.group(1) is not None else html.unescape(mm.group(2) or "")
        else:
            msg = ""
        throwable = ""
        tm = re.search(r"<log4j:Throwable>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</log4j:Throwable>", raw, re.S)
        if tm:
            throwable = tm.group(1) if tm.group(1) is not None else html.unescape(tm.group(2) or "")
        level = attrs.get("level", "INFO").upper()
        head = "[%s] [%s/%s] [%s]: " % (_fmt_ts(attrs.get("timestamp", 0)), attrs.get("thread", "?"), level,
                                         attrs.get("logger", "?"))
        msg_lines = msg.split("\n")
        out.append(head + msg_lines[0])
        out.extend(msg_lines[1:])
        if throwable:
            out.extend(throwable.split("\n"))
        i = j + 1
    return out


def parse_header(line: str):
    m = _HEADER.match(line)
    if not m:
        return None
    lvl = m.group("level")
    if lvl not in LEVELS:
        return None
    return {"ts": m.group("ts"), "thread": m.group("thread"), "level": lvl, "logger": m.group("logger") or "",
            "msg": m.group("msg")}


_EXC_HEAD = re.compile(r"^(?:Caused by:\s*|Suppressed:\s*)?(?P<cls>(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*(?:Exception|Error|Throwable|Failure)[\w$]*)(?::\s?(?P<msg>.*))?$")
_FRAME = re.compile(r"^\s+at\s+(?P<frame>\S+)\((?P<src>[^)]*)\)(?P<tail>.*)$")


def is_exception_head(line: str):
    m = _EXC_HEAD.match(line.strip())
    return m


class Entry:
    __slots__ = ("idx", "level", "logger", "thread", "msg", "cont", "raw")

    def __init__(self, idx, level, logger, thread, msg, raw):
        self.idx, self.level, self.logger, self.thread, self.msg, self.raw = idx, level, logger, thread, msg, raw
        self.cont = []


def entries(lines):
    out = []
    cur = None
    for i, ln in enumerate(lines):
        h = parse_header(ln)
        if h:
            cur = Entry(i, h["level"], h["logger"], h["thread"], h["msg"], ln)
            out.append(cur)
        else:
            if cur is None:
                cur = Entry(i, None, "", "", ln, ln)
                out.append(cur)
            else:
                cur.cont.append(ln)
    return out


# ----------------------------------------------------------------------------------------------
# normalisation
# ----------------------------------------------------------------------------------------------

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T_ ]\d{2}[:\-.]\d{2}[:\-.]\d{2}(?:[.,]\d+)?Z?\b")
_STAMP = re.compile(r"\b\d{8}T\d{6}Z?\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_MAC = re.compile(r"\b(?:[0-9a-fA-F]{1,2}:){5,}[0-9a-fA-F]{1,2}\b")
_HEX0X = re.compile(r"\b0x[0-9a-fA-F]+\b")
_HEXLONG = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b|\b(?=[0-9A-F]*\d)(?=[0-9A-F]*[A-F])[0-9A-F]{8,}\b")
_HEXTAG = re.compile(r"@[0-9a-f]{5,}\b")
_NUMANY = re.compile(r"\d+(?:\.\d+)*")
_RUNDIR = re.compile(r"(?<=[/\\])(?:feature-plan|smoke|run|test|harness|attempt)[-_][A-Za-z0-9._\-]*\d[A-Za-z0-9._\-]*")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Replace everything that varies between otherwise identical runs: timestamps, uuids, hex ids, numbers."""
    s = _RUNDIR.sub("<run-dir>", text)
    s = _UUID.sub("<uuid>", s)
    s = _ISO.sub("<ts>", s)
    s = _STAMP.sub("<ts>", s)
    s = _DATE.sub("<date>", s)
    s = _MAC.sub("<mac>", s)
    s = _TIME.sub("<time>", s)
    s = _HEX0X.sub("<hex>", s)
    s = _HEXTAG.sub("@<hex>", s)
    s = _HEXLONG.sub("<hex>", s)
    s = _NUMANY.sub("<n>", s)
    s = _WS.sub(" ", s).strip()
    return s


_NORMLOGGER = re.compile(r"\d+")


def signature_for(level, logger, msg):
    return "%s [%s] %s" % (level, _NORMLOGGER.sub("<n>", logger or ""), normalize(msg)[:300])


def _line_sigs(e: Entry):
    """Signatures contributed by one log entry: the WARN/ERROR line itself and any exception heads."""
    sigs = []
    if e.level in ("WARN", "ERROR", "FATAL"):
        sigs.append((e.level, signature_for(e.level, e.logger, e.msg), e.raw))
    heads = []
    mm = is_exception_head(e.msg) if e.msg else None
    if mm:
        heads.append(e.msg)
    for c in e.cont:
        if is_exception_head(c):
            heads.append(c)
    for hline in heads:
        m = is_exception_head(hline)
        cls = m.group("cls")
        body = normalize(m.group("msg") or "")[:240]
        kind = "CAUSE" if hline.strip().startswith("Caused by:") else "EXC"
        sigs.append((e.level or "ERROR", "%s %s: %s" % (kind, cls, body), hline))
    return sigs


def group_log(lines, limit=100000):
    """Group WARN/ERROR/exception signatures: OrderedDict sig -> {count, level, example, first_line}."""
    groups = OrderedDict()
    for e in entries(lines):
        for level, sig, raw in _line_sigs(e):
            g = groups.get(sig)
            if g is None:
                groups[sig] = {"sig": sig, "count": 1, "level": level, "example": raw[:400], "first_line": e.idx + 1}
            else:
                g["count"] += 1
            if len(groups) > limit:
                return groups
    return groups


_LEVEL_ORDER = {"FATAL": 0, "ERROR": 1, "WARN": 2}


def diff_logs(base_lines, cand_lines, cap=300):
    """Signatures present in the candidate that are new relative to the baseline, grouped with counts.

    Also reports `resolved` (in baseline only) and `increased` (present in both, candidate count >= 2x and >= +5),
    both informational. All comparison is on normalised text so timestamps/numbers/hex ids never create diffs.
    """
    bg = group_log(base_lines)
    cg = group_log(cand_lines)
    new = [dict(v) for k, v in cg.items() if k not in bg]
    resolved = [dict(v) for k, v in bg.items() if k not in cg]
    increased = []
    for k, v in cg.items():
        if k in bg:
            b, c = bg[k]["count"], v["count"]
            if c >= 2 * b and c - b >= 5:
                increased.append({"sig": k, "level": v["level"], "base_count": b, "count": c, "example": v["example"]})
    key = lambda g: (_LEVEL_ORDER.get(g["level"], 3), -g["count"], g["first_line"])
    new.sort(key=key)
    resolved.sort(key=key)
    increased.sort(key=lambda g: -(g["count"] - g["base_count"]))
    return {
        "new": new[:cap], "new_total": len(new), "new_occurrences": sum(g["count"] for g in new),
        "resolved": resolved[:cap], "resolved_total": len(resolved),
        "increased": increased[:cap], "increased_total": len(increased),
        "base_signatures": len(bg), "cand_signatures": len(cg),
    }


def count_levels(lines):
    c = {"warn": 0, "error": 0, "fatal": 0, "exceptions": 0, "lines": len(lines)}
    for e in entries(lines):
        if e.level == "WARN":
            c["warn"] += 1
        elif e.level == "ERROR":
            c["error"] += 1
        elif e.level == "FATAL":
            c["fatal"] += 1
        for s in _line_sigs(e):
            if s[1].startswith("EXC "):
                c["exceptions"] += 1
    return c


# ----------------------------------------------------------------------------------------------
# crash signature
# ----------------------------------------------------------------------------------------------

_FATAL_MARKERS = (
    ("crash_report", re.compile(r"-{3,}\s*Minecraft Crash Report\s*-{3,}|Preparing crash report|This crash report has been saved")),
    ("uncaught", re.compile(r'Exception in thread "[^"]+"')),
    ("jvm_fatal", re.compile(r"A fatal error has been detected by the Java Runtime|#\s+SIGSEGV|EXCEPTION_ACCESS_VIOLATION")),
    ("game_crashed", re.compile(r"Game crashed|Failed to start game|Crashing the game")),
    ("oom", re.compile(r"java\.lang\.OutOfMemoryError")),
)

_NOT_MOD_PREFIXES = (
    "java.", "javax.", "jdk.", "sun.", "com.sun.", "net.minecraft.", "com.mojang.", "org.lwjgl.", "cpw.mods.",
    "net.neoforged.", "net.minecraftforge.", "org.spongepowered.", "io.netty.", "com.google.", "it.unimi.",
    "org.apache.", "org.objectweb.", "joptsimple.", "org.slf4j.", "org.jline.", "com.electronwill.", "org.sinytra.",
    "com.llamalad7.", "org.openjdk.", "org.joml.", "org.antlr.", "org.codehaus.", "org.tinylog.",
)
_MIXIN_FROM = re.compile(r"(?:from mod|from mod\s+)\s+([a-z0-9_.\-]+)", re.I)
_MIXIN_CFG = re.compile(r"([A-Za-z0-9_.\-]+)\.mixins?\.json:([\w.$]+)")
_HANDLER = re.compile(r"handler\$[a-z0-9]{3,8}\$([a-z0-9_]+)\$")
_JAR = re.compile(r"~\[([^\]:]+?)(?::[^\]]*)?\]")
_MOD_ID_FROM_JAR = re.compile(r"^(.*?)(?:[-_]neoforge|[-_]forge|[-_]fabric|[-_]\d).*$")


def _culprit(trace_lines):
    """Guess the responsible mod/mixin from a stack trace and nearby text.

    Stack frames carry a `{pl:mixin:APP:x.mixins.json:Y from mod Z}` suffix listing every mixin that touched the
    class, so `from mod` is only trusted on non-frame lines of a trace that is itself a mixin/injection error.
    """
    plain = [ln for ln in trace_lines if not _FRAME.match(ln)]
    ptext = "\n".join(plain)
    if re.search(r"(?i)mixin|injection", ptext):
        mm = _MIXIN_FROM.search(ptext)
        if mm:
            return {"kind": "mixin", "mod": mm.group(1), "evidence": "mixin error text names 'from mod %s'" % mm.group(1)}
        cfg = _MIXIN_CFG.search(ptext)
        if cfg:
            return {"kind": "mixin", "mod": cfg.group(1),
                    "evidence": "mixin config %s.mixins.json, class %s" % (cfg.group(1), cfg.group(2))}
    hm = _HANDLER.search(ptext) or next((h for h in (_HANDLER.search(ln) for ln in trace_lines if _FRAME.match(ln)) if h), None)
    if hm:
        return {"kind": "mixin", "mod": hm.group(1), "evidence": "mixin handler frame for mod %s" % hm.group(1)}
    for ln in trace_lines:
        fm = _FRAME.match(ln)
        if not fm:
            continue
        frame = fm.group("frame").rsplit("/", 1)[-1]   # drop `java.base/` and `TRANSFORMER/mod@ver/` prefixes
        if frame.startswith(_NOT_MOD_PREFIXES):
            continue
        jar = _JAR.search(fm.group("tail") or "")
        cls = frame.rsplit(".", 1)[0]
        mod = None
        if jar and jar.group(1) not in ("?", ""):
            base = re.sub(r"\.jar.*$", "", jar.group(1))
            mm2 = _MOD_ID_FROM_JAR.match(base)
            mod = (mm2.group(1) if mm2 and mm2.group(1) else base)
        return {"kind": "mod-frame", "mod": mod or ".".join(cls.split(".")[:4]),
                "evidence": "first non-vanilla frame %s" % frame}
    return {"kind": None, "mod": None, "evidence": "no mod frame in trace (vanilla/loader/JDK only)"}


def crash_signature(lines):
    """First exception in the log, its root cause, a culprit guess, and whether the log shows a fatal marker.

    Returns None when the log contains no exception at all.
    """
    markers = []
    for idx, ln in enumerate(lines):
        for name, rx in _FATAL_MARKERS:
            if rx.search(ln):
                markers.append({"marker": name, "line": idx + 1, "text": ln.strip()[:200]})
                break
        if len(markers) >= 8:
            break
    first = None
    for idx, ln in enumerate(lines):
        # exception heads at line start, or embedded in a log header's message
        h = parse_header(ln)
        text = h["msg"] if h else ln
        m = is_exception_head(text)
        if m and not text.lstrip().startswith("Caused by:"):
            first = (idx, text, m)
            break
    if first is None:
        if markers:
            return {"fatal": True, "markers": markers, "exception": None, "message": None, "line": markers[0]["line"],
                    "root_cause": None, "culprit": {"kind": None, "mod": None, "evidence": "fatal marker without a Java exception"},
                    "frames": []}
        return None
    idx, text, m = first
    # trace = following indented / Caused by lines
    trace = []
    root = None
    j = idx + 1
    while j < len(lines) and len(trace) < 400:
        ln = lines[j]
        if parse_header(ln) is not None:
            break
        if ln.startswith("\t") or ln.startswith("    at ") or ln.lstrip().startswith(("at ", "Caused by:", "... ")) or not ln.strip():
            if not ln.strip():
                break
            trace.append(ln)
            cm = re.match(r"^\s*Caused by:\s*(.*)$", ln)
            if cm:
                root = cm.group(1).strip()
        else:
            if not ln.lstrip().startswith("Caused by:"):
                # message continuation of the exception text
                trace.append(ln)
            else:
                trace.append(ln)
                root = ln.split(":", 1)[1].strip()
        j += 1
    frames = [t.strip() for t in trace if _FRAME.match(t)][:8]
    culprit = _culprit(trace + [text])
    return {
        "fatal": bool(markers), "markers": markers, "exception": m.group("cls"), "message": (m.group("msg") or "")[:400],
        "line": idx + 1, "root_cause": root, "culprit": culprit, "frames": frames,
    }


# ----------------------------------------------------------------------------------------------
# environment extraction from logs
# ----------------------------------------------------------------------------------------------

_MODLINE = re.compile(r"^\s+(?P<name>\S.*?)\s+(?P<ver>\S+)\s+\((?P<id>[A-Za-z0-9_.\-]+)\)\s*$")


def parse_mod_list(lines):
    """The `Mod List:` block FML prints: `Name Version (Mod Id)`. Returns [{id, version, name}]."""
    mods = []
    started = False
    for ln in lines:
        if not started:
            if ln.strip().startswith("Mod List:"):
                started = True
            continue
        if not ln.strip() or ln.strip().startswith("Name Version"):
            continue
        m = _MODLINE.match(ln)
        if m:
            mods.append({"id": m.group("id"), "version": m.group("ver"), "name": m.group("name")})
        elif mods:
            break
    return mods


def log_environment(lines):
    """OS / java / renderer / window facts found in the log text."""
    env = {}
    for ln in lines[:6000]:
        if "os" not in env:
            m = re.search(r"OS (\w+) arch (\w+) version ([^\s\]]+)", ln)
            if m:
                env["os"] = "%s %s %s" % m.groups()
        if "java" not in env:
            m = re.search(r"JVM identified as (.+?)$", ln)
            if m:
                env["java"] = m.group(1).strip()[:160]
        if "gl_renderer" not in env:
            m = re.search(r"OpenGL Renderer:\s*(.+)$", ln)
            if m:
                env["gl_renderer"] = m.group(1).strip()
        if "minecraft" not in env:
            m = re.search(r"--fml\.mcVersion,\s*([\d.]+)", ln)
            if m:
                env["minecraft"] = m.group(1)
        if "loader_version" not in env:
            m = re.search(r"--fml\.neoForgeVersion,\s*([\w.\-]+)", ln)
            if m:
                env["loader"] = "neoforge"
                env["loader_version"] = m.group(1)
        if "window" not in env:
            m = re.search(r"--width,\s*(\d+),\s*--height,\s*(\d+)", ln)
            if m:
                env["window"] = [int(m.group(1)), int(m.group(2))]
        if len(env) >= 7:
            break
    return env
