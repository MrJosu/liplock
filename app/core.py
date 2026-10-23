"""
LipLock — core.

* get_info          — tracks and properties of a file (mkvmerge -J)
* analyze           — offset and speed difference by audio cross-correlation
* relative_offsets  — per-track misalignment inside a donor
* suggest_take      — which donor tracks to tick by the rules
* run_job           — mux the result (re-encoding on PAL speed differences), sorted tracks
* post_check        — listens to the result and verifies every new audio track
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

import numpy as np

from i18n import t, M, render, say, join

os.environ.setdefault("LANG", "C.UTF-8")      # mkvmerge takes its charset from the locale
if not os.environ["LANG"]:
    os.environ["LANG"] = "C.UTF-8"

def _tool(name):
    """Bundled binary (Windows build keeps ffmpeg and mkvmerge in bin/) or the one on PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in (os.environ.get("LIPLOCK_BIN"), os.path.join(getattr(sys, "_MEIPASS", ""), "bin"),
              os.path.join(os.path.dirname(sys.executable), "bin")):
        if d and os.path.isfile(os.path.join(d, exe)):
            return os.path.join(d, exe)
    return shutil.which(name) or name


FFMPEG = _tool("ffmpeg")
MKVMERGE = _tool("mkvmerge")
# on Windows child processes must not pop up console windows
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}

SR = 8000                        # analysis sample rate, mono
CHUNK = 45                       # seconds of donor audio per point
SCAN_WINDOW = 60                 # ± s search window on the first points
WINDOW = 20                      # ± s search window around the prediction
SCAN_POINTS = [0.04, 0.08]
SCAN_POINTS_MORE = [0.15, 0.3]   # if the start differs too much (recap, long intro)
MAX_SCAN_WINDOW = 600            # ± s at most, when the files differ a lot in length
POINTS = [0.2, 0.35, 0.5, 0.65, 0.8, 0.93]
CHECK_POINTS = [0.12, 0.3, 0.5, 0.7, 0.9]
TOLERANCE_MS = 40
MIN_CONFIDENCE = 6.0
REENCODE_ABOVE = 0.002           # speed difference above which audio is re-encoded
REL_APPLY_MS = 20                # smaller in-donor misalignment is ignored

NTSC = 24000 / 1001
CANDIDATES = [
    {"key": "same",      "factor": 1.0},
    {"key": "24>23.976", "factor": 1001 / 1000},
    {"key": "23.976>24", "factor": 1000 / 1001},
    {"key": "25>23.976", "factor": 25 / NTSC},
    {"key": "23.976>25", "factor": NTSC / 25},
    {"key": "25>24",     "factor": 25 / 24},
    {"key": "24>25",     "factor": 24 / 25},
]


def speed_spec(key, factor=1.0):
    return M("speed.custom", f=factor) if key == "custom" else M("speed." + key)


def speed_label(key, factor=1.0):
    return render(speed_spec(key, factor))


VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mts", ".mov", ".webm", ".wmv"}
AUDIO_EXT = {".mka", ".ac3", ".eac3", ".ec3", ".dts", ".thd", ".truehd", ".flac", ".aac",
             ".m4a", ".mp3", ".wav", ".opus", ".ogg"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sup", ".idx", ".vtt"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT | SUB_EXT


class Cancelled(Exception):
    pass


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ---------------------------------------------------------------- file info

def get_info(path):
    res = subprocess.run([MKVMERGE, "-J", path], capture_output=True, timeout=180, **NOWIN)
    try:
        data = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        data = {}
    cont = data.get("container", {})
    if not cont.get("recognized") or not cont.get("supported", True):
        raise ValueError(t("e.format", name=os.path.basename(path)))

    tracks, pixels = [], 0
    for tr in data.get("tracks", []):
        p = tr.get("properties", {})
        dims = p.get("pixel_dimensions") or ""
        if tr.get("type") == "video" and "x" in dims:
            w, h = dims.split("x")[:2]
            if w.isdigit() and h.isdigit():
                pixels = max(pixels, int(w) * int(h))
        tracks.append({
            "id": tr["id"],
            "type": tr.get("type"),
            "codec": tr.get("codec", ""),
            "lang": p.get("language") or "und",
            "name": p.get("track_name") or "",
            "channels": p.get("audio_channels"),
            "sr": p.get("audio_sampling_frequency"),
            "default": bool(p.get("default_track")),
            "forced": bool(p.get("forced_track")),
            "dims": dims,
        })
    dur_ns = cont.get("properties", {}).get("duration")
    return {
        "path": path,
        "name": os.path.basename(path),
        "size": os.path.getsize(path),
        "duration": dur_ns / 1e9 if dur_ns else None,
        "pixels": pixels,
        "tracks": tracks,
    }


def track_label(tr):
    parts = [tr.get("codec") or "?", tr.get("lang") or "und"]
    if tr.get("channels"):
        parts.append(f"{tr['channels']}ch")
    if tr.get("name"):
        parts.append(f"«{tr['name']}»")
    return " ".join(parts)


# ---------------------------------------------------------------- analysis

def _extract(path, tid, start, dur):
    # Seek a little earlier, then trim sample-accurately: a plain -ss lands on an
    # audio frame boundary (up to 32 ms off for E-AC3).
    start = max(0.0, start)
    pre = start - 2
    seek = ["-ss", f"{pre:.3f}"] if pre > 0 else []   # "-ss 0" together with -copyts breaks the start
    cmd = [FFMPEG, "-v", "error", *seek, "-copyts", "-i", path,
           "-map", f"0:{tid}", "-af", f"atrim=start={start:.4f}:duration={dur:.4f}",
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=600, **NOWIN).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return x - (x.mean() if len(x) else 0), start


def _stretch(a, f):
    if abs(f - 1) < 1e-9:
        return a
    n = int(len(a) * f)
    return np.interp(np.arange(n) / f, np.arange(len(a)), a).astype(np.float32)


def _correlate(a, a_start, b, b_start, f):
    a = _stretch(a, f)
    if len(a) < SR * 5 or len(b) <= len(a):
        return None
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    valid = corr[: len(b) - len(a) + 1]
    k = int(np.argmax(valid))
    conf = float((valid[k] - valid.mean()) / (valid.std() + 1e-9))
    return ((b_start + k / SR) - a_start) * 1000.0, conf


def _grab(src, src_ref, dst, dst_ref, at, center_ms, window):
    a, a_start = _extract(src, src_ref, at, CHUNK)
    b, b_start = _extract(dst, dst_ref, at + center_ms / 1000 - window, CHUNK * 1.05 + 2 * window)
    return a, a_start, b, b_start


def _offsets(points, f):
    return np.array([p[1] - (f - 1) * p[0] * 1000 for p in points])


def _fmt_t(sec):
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}:{m:02d}" if h else t("min", m=m)


def analyze(src, src_ref, dst, dst_ref, dur, log=lambda m: None, cancel=None, dst_dur=None):
    """Offset (ms) and speed factor of a donor track relative to the main file."""
    log(t("an.start"))
    # A big length difference usually means extra material at the start (recap, logos):
    # the offset can then exceed the normal search window, so widen it.
    win = SCAN_WINDOW
    if dur and dst_dur and abs(dst_dur - dur) > SCAN_WINDOW - 15:
        win = min(MAX_SCAN_WINDOW, abs(dst_dur - dur) + 30)

    def scan(fracs):
        scores = [0.0] * len(CANDIDATES)
        found = [[] for _ in CANDIDATES]
        for frac in fracs:
            _check(cancel)
            a, a0, b, b0 = _grab(src, src_ref, dst, dst_ref, dur * frac, 0, win)
            for i, c in enumerate(CANDIDATES):
                r = _correlate(a, a0, b, b0, c["factor"])
                if r:
                    scores[i] += r[1]
                    found[i].append((a0, r[0], r[1]))
        best = int(np.argmax(scores))
        return best, [p for p in found[best] if p[2] >= MIN_CONFIDENCE]

    best, points = scan(SCAN_POINTS)
    if not points:
        log(t("an.scan.more"))
        best, points = scan(SCAN_POINTS_MORE)
    cand = CANDIDATES[best]
    f = cand["factor"]
    result = {"ok": False, "status": "no_match", "delay": None, "factor": f, "speed": cand["key"],
              "label": speed_label(cand["key"]), "label_t": speed_spec(cand["key"]), "points": [], "message": ""}
    if not points:
        result.update(factor=1.0, speed="same")
        say(result, "label", M("an.nomatch.label"))
        say(result, "message", M("an.nomatch.msg", win=int(win)))
        log(result["message"])
        return result
    log(t("an.speed", label=result["label"]))

    missed = 0
    for frac in POINTS:
        _check(cancel)
        at = dur * frac
        t_ref, d_ref, _ = points[-1]
        predicted = d_ref + (f - 1) * (at - t_ref) * 1000
        # a point whose audio runs past the end of either file proves nothing — skip it, not a miss
        if at + CHUNK > dur or (dst_dur and at + predicted / 1000 + CHUNK * f > dst_dur):
            continue
        a, a0, b, b0 = _grab(src, src_ref, dst, dst_ref, at, predicted, WINDOW)
        r = _correlate(a, a0, b, b0, f)
        if r and r[1] >= MIN_CONFIDENCE:
            points.append((a0, r[0], r[1]))
        else:
            missed += 1            # after a big insert the audio leaves the search window

    # Recheck outliers: when different translations are compared only the background matches,
    # and a single measurement can lock onto the wrong place. Measure again nearby.
    rechecked = 0
    if len(points) >= 5:
        o = _offsets(points, f)
        med = float(np.median(o))
        bad = [i for i in range(len(points)) if abs(o[i] - med) > TOLERANCE_MS]
        if len(bad) <= 2:
            for i in bad:
                t0 = points[i][0]
                for shift in (75, -75, 150):
                    _check(cancel)
                    tt = t0 + shift
                    if tt <= 5 or tt >= dur - CHUNK - 5:
                        continue
                    a, a0, b, b0 = _grab(src, src_ref, dst, dst_ref, tt, med + (f - 1) * tt * 1000, WINDOW)
                    r = _correlate(a, a0, b, b0, f)
                    if r and r[1] >= MIN_CONFIDENCE and abs(r[0] - (f - 1) * a0 * 1000 - med) <= TOLERANCE_MS:
                        log(t("an.recheck", a=_fmt_t(t0), b=_fmt_t(a0)))
                        points[i] = (a0, r[0], r[1])
                        rechecked += 1
                        break
            points.sort(key=lambda p: p[0])

    result["points"] = [{"t": round(s, 1), "delay": int(round(d)), "conf": round(c, 1)}
                        for s, d, c in points]
    s = np.array([p[0] for p in points])
    d = np.array([p[1] for p in points])
    offsets = _offsets(points, f)
    med = float(np.median(offsets))
    result["delay"] = int(round(med))
    outliers = [i for i in range(len(points)) if abs(offsets[i] - med) > TOLERANCE_MS]
    note = M("an.recheck1") if rechecked == 1 else (M("an.recheckN", n=rechecked) if rechecked else "")

    if len(points) < 3:
        result.update(status="few_points")
        say(result, "message", M("an.few"))
    elif np.ptp(offsets) <= TOLERANCE_MS:
        result.update(ok=True, status="ok")
        say(result, "message", M("an.stable", spread=f"{np.ptp(offsets):.0f}", note=note))
    elif len(points) >= 5 and len(outliers) == 1:
        # an edit difference would move every point after it; here the rest agree
        i = outliers[0]
        inl = np.delete(offsets, i)
        key = "an.out.first" if i == 0 else ("an.out.last" if i == len(points) - 1 else "an.out.mid")
        result.update(ok=True, status="outlier", delay=int(round(float(np.median(inl)))))
        say(result, "message", M(key, where=_fmt_t(points[i][0])))
    else:
        m, c = np.polyfit(s, d, 1)
        resid = d - (m * s + c)
        if np.ptp(resid) <= TOLERANCE_MS:
            f2 = float(1 + m / 1000)
            result.update(ok=True, status="custom_drift", delay=int(round(c)), factor=f2, speed="custom")
            say(result, "label", speed_spec("custom", f2))
            say(result, "message", M("an.drift"))
        else:
            result.update(status="inconsistent")
            say(result, "message", M("an.inconsistent"))
    if missed >= 2 and result["ok"]:
        result.update(ok=False, status="few_points")
        say(result, "message", M("an.missed", missed=missed, total=len(POINTS)))
    log(t("an.result", delay=result["delay"], f=result["factor"], msg=result["message"]))
    return result


def relative_offsets(path, base_id, ids, dur, log=lambda m: None, cancel=None, names=None):
    """How much each donor track is shifted against the reference track of the same file.
    Releasers sometimes mux a voice-over with an offset — then it needs its own correction.
    Returns {str(id): {"rel": ms, "status": ok|unknown}}."""
    out = {}
    for tid in ids:
        if tid == base_id:
            out[str(tid)] = {"rel": 0, "status": "ok"}
            continue
        vals = []
        for frac in CHECK_POINTS:
            _check(cancel)
            a, a0, b, b0 = _grab(path, tid, path, base_id, dur * frac, 0, 5)
            r = _correlate(a, a0, b, b0, 1.0)
            if r and r[1] >= MIN_CONFIDENCE:
                vals.append(r[0])
        if len(vals) >= 3:
            med = float(np.median(vals))
            inl = [v for v in vals if abs(v - med) <= TOLERANCE_MS]
            if len(inl) >= 3:
                rel = int(round(float(np.median(inl))))
                rel = rel if abs(rel) > REL_APPLY_MS else 0
                out[str(tid)] = {"rel": rel, "status": "ok"}
                if rel:
                    log(t("an.rel", name=(names or {}).get(tid, f"[{tid}]"),
                          dir=t("an.rel.late") if rel < 0 else t("an.rel.early"), ms=abs(rel)))
                continue
        out[str(tid)] = {"rel": 0, "status": "unknown"}
    return out


# ---------------------------------------------------------------- series: pair episodes

_EP_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[ ._-]?[EeЕе](\d{1,3})(?!\d)"),          # S01E02, s1e2, S01.E02
    re.compile(r"(?<!\d)(\d{1,2})[xXхХ](\d{2,3})(?!\d)"),              # 1x02
    re.compile(r"(?<![A-Za-z])[Ee][Pp]?(\d{2,3})(?!\d)"),               # E02, Ep02
    re.compile(r"(?:[Сс]ерия|[Ээ]пизод|[Ee]pisode)[ ._-]?(\d{1,3})(?!\d)"),
]


def episode_code(path):
    """S01E02 / E02 from a file name, or None."""
    name = os.path.splitext(os.path.basename(path))[0]
    for i, rx in enumerate(_EP_PATTERNS):
        m = rx.search(name)
        if m:
            if i < 2:
                return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
            return f"E{int(m.group(1)):02d}"
    return None


def _same_episode(a, b):
    if a == b:
        return True
    ea, eb = a[a.index("E"):], b[b.index("E"):]          # one of them may lack the season
    return ea == eb and (a.startswith("E") or b.startswith("E"))


def _natural(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", os.path.basename(s))]


def pair_episodes(mains, donors):
    """Pairs main files with donor files by episode number.
    Returns (pairs, leftover_donors) where pairs = [(code, main, [donors])], or None if unclear."""
    mc = {m: episode_code(m) for m in mains}
    dc = {d: episode_code(d) for d in donors}
    if all(mc.values()) and len(set(mc.values())) == len(mains):
        pairs, used = [], set()
        for m in sorted(mains, key=lambda x: mc[x]):
            ds = [d for d in donors if dc[d] and _same_episode(dc[d], mc[m])]
            used.update(ds)
            pairs.append((mc[m], m, ds))
        return pairs, [d for d in donors if d not in used]
    if len(mains) == len(donors):           # no numbers in names: same count, natural sort order
        ms, ds = sorted(mains, key=_natural), sorted(donors, key=_natural)
        return [(f"#{i + 1:02d}", m, [d]) for i, (m, d) in enumerate(zip(ms, ds))], []
    return None


# ---------------------------------------------------------------- draft suggestions

def _norm(name):
    return re.sub(r"\W+", "", (name or "").lower())


def choose_ref(donor_audio, main_audio, prio=("rus", "eng")):
    """Pair of tracks to compare: same voice-over → original (eng) → any common language."""
    for dt in donor_audio:
        for mt in main_audio:
            if dt["lang"] == mt["lang"] and _norm(dt["name"]) and _norm(dt["name"]) == _norm(mt["name"]):
                return dt["id"], mt["id"]
    main_langs = {x["lang"] for x in main_audio}
    order = ["eng"] + [x for x in prio if x != "eng"] + [x["lang"] for x in donor_audio]
    for lang in order:
        if lang in main_langs and any(x["lang"] == lang for x in donor_audio):
            return (next(x["id"] for x in donor_audio if x["lang"] == lang),
                    next(x["id"] for x in main_audio if x["lang"] == lang))
    return donor_audio[0]["id"], main_audio[0]["id"]


def track_key(tr):
    return (tr["type"], tr["lang"], tr["codec"], tr.get("channels"))


def suggest_take(donor_info, existing, settings):
    """Which donor tracks to tick by the rules. existing — set of track_key already present."""
    langs = settings.get("langs") or []
    remove = settings.get("remove_langs") or []
    out = []
    for tr in donor_info["tracks"]:
        if tr["type"] not in ("audio", "subtitles") or tr["lang"] in remove:
            continue
        if tr["type"] == "subtitles" and not settings.get("take_subs", True):
            continue
        if langs and tr["lang"] not in langs:
            continue
        if settings.get("mode", "new") == "new" and track_key(tr) in existing:
            continue
        out.append(tr)
    return out


# ---------------------------------------------------------------- muxing

def run_process(cmd, on_line, cancel=None):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **NOWIN)
    if cancel is not None:
        def killer():
            while proc.poll() is None:
                if cancel.wait(0.5):
                    proc.terminate()
                    return
        threading.Thread(target=killer, daemon=True).start()
    tail, buf = [], b""
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                tail = (tail + [line])[-40:]
                on_line(line)
    proc.wait()
    _check(cancel)
    return proc.returncode, tail


# codecs many players and TVs cannot pass through as is (Apple TV, many smart TVs and soundbars)
COMPAT_RE = re.compile(r"DTS|TrueHD|MLP|FLAC|PCM", re.I)


def needs_compat(track):
    return track.get("type") == "audio" and bool(COMPAT_RE.search(track.get("codec") or ""))


def _reencode(src, track, factor, out_path, duration, on_frac, cancel):
    """Re-encode one track to E-AC3. factor != 1 also corrects the speed (PAL); factor == 1 only
    converts the format. E-AC3 allows up to 5.1 and 32/44.1/48 kHz, so 7.1 and 96 kHz are reduced."""
    sr = int(track.get("sr") or 48000)
    ch = int(track.get("channels") or 2)
    out_sr = sr if sr in (32000, 44100, 48000) else 48000
    if abs(factor - 1) > 1e-9:
        new_rate = round(sr / factor)
        residual = factor / (sr / new_rate)    # rounding error — fixed via timestamps
        af = ["-af", f"asetrate={new_rate},aresample={out_sr}"]
    else:
        residual = 1.0
        af = ["-ar", str(out_sr)]
    ac = min(ch, 6)
    br = "640k" if ac >= 6 else ("256k" if ac >= 2 else "128k")
    total_us = (duration or 0) * factor * 1e6
    cmd = [FFMPEG, "-v", "error", "-nostats", "-progress", "pipe:1", "-y", "-i", src,
           "-map", f"0:{track['id']}", *af,
           "-c:a", "eac3", "-b:a", br, "-ac", str(ac), "-f", "matroska", out_path]

    def on_line(line):
        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if m and total_us:
            on_frac(min(1.0, int(m.group(1)) / total_us))

    code, tail = run_process(cmd, on_line, cancel)
    if code != 0:
        raise RuntimeError("ffmpeg: " + " | ".join(tail[-5:]))
    return residual, ac


def _sync(tid, delay, factor):
    if abs(factor - 1) < 1e-9:
        return ["--sync", f"{tid}:{int(delay)}"]
    return ["--sync", f"{tid}:{int(delay)},{factor:.9f}"]


def _sort_entries(entries, order_langs):
    """Order of audio or subtitle tracks: language priority, default track first, then original order."""
    prio = {lang: i for i, lang in enumerate(order_langs or [])}
    return sorted(enumerate(entries), key=lambda ie: (prio.get(ie[1]["lang"], len(prio)),
                                                     0 if ie[1].get("default") else 1, ie[0]))


def run_job(plan, output, tmp_dir, log, progress, cancel=None):
    """
    plan = {
      main, main_info, main_audio_keep, main_subs_keep,
      default_audio ({"src": "main"|donor_index, "id": tid} | None),
      donors: [{path, info, delay, factor, tracks: [{id, type, lang, name, delay?, ...}]}],
      order_langs: ["rus", "eng", ...], check_ref,
      compat: "off" | "replace" | "add"   — DTS/TrueHD/FLAC/PCM from donors → E-AC3
    }
    Returns (output, check).
    """
    main_info = plan["main_info"]
    by_id = {x["id"]: x for x in main_info["tracks"]}
    main_audio = [x["id"] for x in main_info["tracks"] if x["type"] == "audio"]
    main_subs = [x["id"] for x in main_info["tracks"] if x["type"] == "subtitles"]
    main_video = [x["id"] for x in main_info["tracks"] if x["type"] == "video"]
    keep_audio = main_audio if plan.get("main_audio_keep") is None else plan["main_audio_keep"]
    keep_subs = main_subs if plan.get("main_subs_keep") is None else plan["main_subs_keep"]
    dflt = plan.get("default_audio")

    def is_default(src, tid):
        return bool(dflt) and dflt.get("src") == src and dflt.get("id") == tid

    compat = plan.get("compat") or "off"

    def converted(d):
        """Audio tracks of a donor that get an E-AC3 version (speed fix or compatibility)."""
        if abs(d["factor"] - 1) > REENCODE_ABOVE:
            return [x for x in d["tracks"] if x["type"] == "audio"]
        if compat in ("replace", "add"):
            return [x for x in d["tracks"] if needs_compat(x)]
        return []

    steps = sum(len(converted(d)) for d in plan["donors"]) + 1
    done = [0]

    def step_progress(frac, phase):
        progress(min(98.0, (done[0] + frac) / steps * 98), phase)

    # every audio/subtitle track of the result: file id, track id, language, default, new?
    audio, subs = [], []
    for tid in keep_audio:
        audio.append({"fid": 0, "tid": tid, "lang": by_id[tid]["lang"], "new": False,
                      "default": is_default("main", tid) if dflt else by_id[tid]["default"],
                      "name": track_label(by_id[tid])})
    for tid in keep_subs:
        subs.append({"fid": 0, "tid": tid, "lang": by_id[tid]["lang"], "default": by_id[tid]["default"]})
    fid = 0

    os.makedirs(tmp_dir, exist_ok=True)
    part = output + ".part"
    try:
        cmd = [MKVMERGE, "--gui-mode", "-o", part]
        if set(keep_audio) != set(main_audio):
            cmd += ["-a", ",".join(map(str, keep_audio))] if keep_audio else ["--no-audio"]
        if set(keep_subs) != set(main_subs):
            cmd += ["-s", ",".join(map(str, keep_subs))] if keep_subs else ["--no-subtitles"]
        if dflt:
            for tid in keep_audio:
                cmd += ["--default-track-flag", f"{tid}:{1 if is_default('main', tid) else 0}"]
        cmd.append(plan["main"])

        for i, d in enumerate(plan["donors"]):
            big = abs(d["factor"] - 1) > REENCODE_ABOVE
            a_tr = [x for x in d["tracks"] if x["type"] == "audio"]
            s_tr = [x for x in d["tracks"] if x["type"] == "subtitles"]
            conv = converted(d)
            conv_ids = {x["id"] for x in conv}
            keep_orig = not big and compat == "add"          # the original stays next to its E-AC3 copy
            direct = s_tr + [x for x in a_tr if x["id"] not in conv_ids or keep_orig]

            for x in conv:
                _check(cancel)
                phase = t("b.reencode", id=x["id"], file=os.path.basename(d["path"]))
                log(phase + t("b.reencode.why" if big else "b.compat.why"))
                tmp = os.path.join(tmp_dir, f"d{i}_t{x['id']}.mka")
                dur = (d.get("info") or {}).get("duration") or main_info.get("duration")
                residual, ac = _reencode(d["path"], x, d["factor"] if big else 1.0, tmp, dur,
                                         lambda fr, ph=phase: step_progress(fr, ph), cancel)
                done[0] += 1
                if big:
                    name = (t("b.name.fixed", name=x["name"]) if x.get("name")
                            else t("b.name.fixed2", ch=ac, codec=x.get("codec", "audio")))
                    stretch = residual
                else:
                    name = (t("b.name.compat", name=x["name"]) if x.get("name")
                            else t("b.name.compat2", ch=ac, codec=x.get("codec", "audio")))
                    stretch = d["factor"]                  # small speed difference stays in timestamps
                fid += 1
                audio.append({"fid": fid, "tid": 0, "lang": x.get("lang") or "und", "new": True,
                              "default": is_default(i, x["id"]), "name": name})
                cmd += _sync(0, x.get("delay", d["delay"]), stretch)
                cmd += ["--language", f"0:{x.get('lang') or 'und'}", "--track-name", f"0:{name}",
                        "--default-track-flag", f"0:{1 if is_default(i, x['id']) else 0}", tmp]

            if not direct:
                continue
            fid += 1
            cmd += ["--no-video", "--no-chapters", "--no-attachments", "--no-global-tags", "--no-track-tags"]
            a_ids = [x["id"] for x in direct if x["type"] == "audio"]
            s_ids = [x["id"] for x in direct if x["type"] == "subtitles"]
            cmd += ["-a", ",".join(map(str, a_ids))] if a_ids else ["--no-audio"]
            cmd += ["-s", ",".join(map(str, s_ids))] if s_ids else ["--no-subtitles"]
            for x in sorted(direct, key=lambda y: y["id"]):
                entry = {"fid": fid, "tid": x["id"], "lang": x.get("lang") or "und"}
                if x["type"] == "audio":
                    audio.append({**entry, "new": True,
                                  "default": is_default(i, x["id"]) and x["id"] not in conv_ids,
                                  "name": x.get("name") or track_label(x)})
                else:
                    subs.append({**entry, "default": False})
            for x in direct:
                tid = x["id"]
                cmd += _sync(tid, x.get("delay", d["delay"]), d["factor"])
                cmd += ["--language", f"{tid}:{x.get('lang') or 'und'}"]
                if x.get("name"):
                    cmd += ["--track-name", f"{tid}:{x['name']}"]
                if x["type"] == "audio":
                    flag = is_default(i, tid) and tid not in conv_ids
                    cmd += ["--default-track-flag", f"{tid}:{1 if flag else 0}"]
            cmd.append(d["path"])

        # final track order: video, audio by language priority, subtitles by language priority
        audio_sorted = [e for _, e in _sort_entries(audio, plan.get("order_langs"))]
        subs_sorted = [e for _, e in _sort_entries(subs, plan.get("order_langs"))]
        order = [f"0:{v}" for v in main_video] + [f"{e['fid']}:{e['tid']}" for e in audio_sorted + subs_sorted]
        cmd[4:4] = ["--track-order", ",".join(order)]   # после "-o part"
        log(t("b.order", order=", ".join(f"{e['lang']}" + ("*" if e.get("default") else "")
                                           for e in audio_sorted)))

        log(t("b.muxing", name=os.path.basename(output)))

        def on_line(line):
            m = re.search(r"progress\s+(\d+)%", line, re.I) or re.search(r"Progress:\s*(\d+)%", line)
            if m:
                step_progress(int(m.group(1)) / 100, t("b.phase.mux"))
            elif line.startswith("#GUI#"):
                kind, _, rest = line[5:].partition(" ")
                if kind == "warning":
                    log(t("b.warning", msg=rest.split("#")[0]))
                elif kind == "error":
                    log(t("b.error", msg=rest.split("#")[0]))

        code, tail = run_process(cmd, on_line, cancel)
        if code not in (0, 1):
            raise RuntimeError("mkvmerge: " + " | ".join(x for x in tail[-6:] if "progress" not in x.lower()))
        os.replace(part, output)
        log(t("b.done", path=output))
        progress(99.0, t("b.phase.check"))
        try:
            check = post_check(output, plan, audio_sorted, log, cancel)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            check = say({"ok": None, "items": []}, "message", M("b.check.failed", err=str(e)))
            log(check["message"])
        progress(100.0, t("b.phase.done"))
        return output, check
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------- video integrity

def video_profile(path):
    """Codec, bit depth, size, HDR transfer and Dolby Vision profile of the first video stream."""
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, timeout=120, **NOWIN)
    except Exception:  # noqa: BLE001
        return None
    lines = res.stderr.decode("utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if "Video:" not in line or "Stream #" not in line or "attached pic" in line:
            continue
        m = re.search(r"Video: (\w+)(?: \(([^)]*)\))?.*?, ([a-z0-9]+)(?:\(([^)]*)\))?", line)
        if not m:
            continue
        codec, prof, pix, colors = m.groups()
        colors = colors or ""
        bits = 12 if "12" in pix else 10 if ("10" in pix or "Main 10" in (prof or "")) else 8
        dims = re.search(r", (\d{3,5}x\d{3,5})", line)
        out = {"codec": codec, "bits": bits, "dims": dims.group(1) if dims else None,
               "hdr": "HDR10" if "smpte2084" in colors else "HLG" if "arib-std-b67" in colors else None,
               "dovi": None, "mastering": False}
        for nxt in lines[i + 1:]:
            if "Stream #" in nxt:
                break
            d = re.search(r"DOVI configuration record:.*?profile: (\d+)", nxt)
            if d:
                out["dovi"] = int(d.group(1))
            if "Mastering Display" in nxt:
                out["mastering"] = True
        return out
    return None


def describe_video(v):
    parts = [v["codec"].upper(), M("v.bits", n=v["bits"])]
    if v.get("dims"):
        parts.append(v["dims"])
    parts.append(v["hdr"] or "SDR")
    if v.get("dovi") is not None:
        parts.append(M("v.dv", n=v["dovi"]))
    return join(parts, ", ")


def check_video(main_path, output):
    a, b = video_profile(main_path), video_profile(output)
    if not a or not b:
        return say({"ok": None}, "text", M("v.unknown"))
    keys = ("codec", "bits", "dims", "hdr", "dovi", "mastering")
    if all(a.get(k) == b.get(k) for k in keys):
        return say({"ok": True}, "text", M("v.same", desc=describe_video(b)))
    return say({"ok": False}, "text", M("v.diff", a=describe_video(a), b=describe_video(b)))


# ---------------------------------------------------------------- post-check

def post_check(output, plan, audio_order, log=lambda m: None, cancel=None):
    """Listens to the result: every new audio track must line up with the main file's original.
    audio_order — audio tracks of the result in their final order.
    Returns {"ok": True|False|None, "items": [...], "message": str}."""
    info = get_info(output)
    out_audio = [x for x in info["tracks"] if x["type"] == "audio"]
    res = {"ok": True, "items": [], "message": "", "video": None}
    if any(x["type"] == "video" for x in plan["main_info"]["tracks"]):
        res["video"] = check_video(plan["main"], output)
        log(t("c.log2", msg=res["video"]["text"]))
    res = _post_audio(output, plan, audio_order, info, out_audio, res, log, cancel)
    if res["video"] and res["video"]["ok"] is False:
        res["ok"] = False
        say(res, "message", join([res["video"]["text_t"], res.get("message_t") or res["message"]], " "))
    return res


def _post_audio(output, plan, audio_order, info, out_audio, res, log, cancel):
    if len(out_audio) != len(audio_order):
        res["ok"] = False
        say(res, "message", M("c.count", got=len(out_audio), want=len(audio_order)))
        log(t("c.log2", msg=res["message"]))
        return res
    new = [(k, e) for k, e in enumerate(audio_order) if e["new"]]
    if not new:
        say(res, "message", M("c.nothing"))
        return res

    by_id = {x["id"]: x for x in plan["main_info"]["tracks"]}
    kept = [(k, e) for k, e in enumerate(audio_order) if not e["new"]]
    ref = next(((k, e) for k, e in kept if e["tid"] == plan.get("check_ref")), None)
    if ref is None:
        ref = next(((k, e) for k, e in kept if by_id.get(e["tid"], {}).get("lang") == "eng"), None) \
            or (kept[0] if kept else None)
    if ref is None:
        res["ok"] = None
        say(res, "message", M("c.noref"))
        return res
    ref_out = out_audio[ref[0]]["id"]
    ref_label = track_label(by_id[ref[1]["tid"]])
    dur = info["duration"] or plan["main_info"].get("duration") or 0

    bad = unknown = 0
    for k, meta in new:
        _check(cancel)
        t_out = out_audio[k]
        vals = []
        for frac in CHECK_POINTS:
            a, a0, b, b0 = _grab(output, t_out["id"], output, ref_out, dur * frac, 0, 3)
            r = _correlate(a, a0, b, b0, 1.0)
            if r and r[1] >= MIN_CONFIDENCE:
                vals.append(r[0])
        item = {"name": meta["name"], "points": len(vals), "of": len(CHECK_POINTS)}
        if len(vals) >= 3:
            med = float(np.median(vals))
            inl = [v for v in vals if abs(v - med) <= TOLERANCE_MS]
            item["offset"] = int(round(med))
            if len(inl) >= 3 and abs(med) <= TOLERANCE_MS:
                item["status"] = "ok"
                say(item, "text", M("c.ok", ref=ref_label, n=len(inl), of=len(CHECK_POINTS), ms=med))
            else:
                bad += 1
                # med = time in the original − time in the new track: negative means the new one is late
                text = (M("c.late" if med < 0 else "c.early", ref=ref_label, ms=abs(med))
                        if abs(med) > TOLERANCE_MS else M("c.unstable", n=len(inl), of=len(vals)))
                item["status"] = "warn"
                say(item, "text", text)
        else:
            unknown += 1
            item["status"] = "unknown"
            say(item, "text", M("c.unknown"))
        if meta.get("default") and not t_out.get("default"):
            if item["status"] != "warn":
                bad += 1
            item["status"] = "warn"
            say(item, "text", join([item["text_t"], M("c.flag")]))
        res["items"].append(item)
        log(t("c.log", name=meta["name"], text=item["text"]))

    if bad:
        res["ok"] = False
        say(res, "message", M("c.bad", bad=bad, total=len(new)))
    elif unknown:
        res["ok"] = None
        say(res, "message", M("c.partial"))
    else:
        say(res, "message", M("c.allok"))
    return res
