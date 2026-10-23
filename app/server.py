"""
LipLock — web server, drafts from the watch folder and the build queue.

Watch folder:  WATCH_DIR/<Film>/main/     — exactly one main file
               WATCH_DIR/<Film>/donors/   — one or more donors
Each such folder becomes a draft: tracks are ticked by the rules and the offset is
measured right away. It is built after confirmation (or automatically, if enabled).
"""
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid

from flask import Flask, jsonify, request, send_from_directory

import core
import i18n
from i18n import t

# ================================================================ настройки


def env_bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on", "да")


def env_list(name):
    return [x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()]


def env_choice(name, options):
    """First option is the default; anything not in the list falls back to it."""
    v = os.environ.get(name, "").strip().lower()
    return v if v in options else options[0]


MEDIA_DIRS = [d.strip().rstrip("/") or "/" for d in
              os.environ.get("MEDIA_DIRS", os.environ.get("MEDIA_DIR", "/media")).split(",") if d.strip()]
WATCH_DIR = os.environ.get("WATCH_DIR", "/watch").rstrip("/")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output").rstrip("/")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
PORT = int(os.environ.get("PORT", "47650"))
HOST = os.environ.get("HOST", "0.0.0.0")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
STABLE_SECONDS = int(os.environ.get("STABLE_SECONDS", "60"))

MAIN_DIRS = ("main", "основной")
DONOR_DIRS = ("donors", "доноры")
MARKER = ".liplock.json"
OLD_MARKERS = (".audiosync.json", ".josumux.json")

DEFAULT_SETTINGS = {
    "enabled": env_bool("AUTO_ENABLED", True),
    "mode": os.environ.get("AUTO_MODE", "new"),          # new | all
    "langs": env_list("AUTO_LANGS"),
    "remove_langs": env_list("AUTO_REMOVE_LANGS"),
    "default_lang": os.environ.get("AUTO_DEFAULT_LANG", "").strip().lower(),
    "take_subs": env_bool("AUTO_TAKE_SUBS", True),
    "auto_build": env_bool("AUTO_BUILD", False),
    "after": os.environ.get("AUTO_AFTER", "keep"),        # keep | delete
    "parallel": int(os.environ.get("PARALLEL", "1")),
    "order_langs": env_list("TRACK_ORDER") or ["rus", "eng"],
    "compat": env_choice("COMPAT", ("off", "replace", "add")),   # DTS/TrueHD/FLAC/PCM from donors → E-AC3
    "lang": (os.environ.get("UI_LANG", "ru").strip().lower()[:2] or "ru"),
}
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
JOBS_FILE = os.path.join(CONFIG_DIR, "jobs.json")
DRAFTS_FILE = os.path.join(CONFIG_DIR, "drafts.json")
lock = threading.RLock()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print("save error:", path, e, flush=True)


SETTINGS = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
i18n.set_lang(SETTINGS.get("lang"))

# ================================================================ пути


def roots():
    out, seen = [], set()
    items = [("media", d) for d in MEDIA_DIRS] + [("watch", WATCH_DIR), ("output", OUTPUT_DIR)]
    for kind, path in items:
        if os.path.isdir(path) and path not in seen:
            seen.add(path)
            out.append({"kind": kind, "label": path, "path": path})
    return out


def _inside(path, root):
    """path is root itself or lies under it (works with Windows drives and separators too)."""
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:            # different drives on Windows
        return False


def safe_path(p, must_exist=True):
    if not p:
        raise ValueError(t("e.nopath"))
    real = os.path.realpath(p)
    for r in roots():
        rr = os.path.realpath(r["path"])
        if _inside(real, rr):
            if must_exist and not os.path.exists(real):
                raise ValueError(t("e.notfound", path=p))
            return real
    raise ValueError(t("e.outside"))


def unique_path(path):
    if not os.path.exists(path) and not os.path.exists(path + ".part"):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def clean_name(name, fallback):
    name = os.path.basename((name or "").strip()) or fallback
    for ch in '<>:"|?*\\/':
        name = name.replace(ch, "_")
    if not name.lower().endswith(".mkv"):
        name += ".mkv"
    return name


def factor_of(d):
    if d.get("speed") == "custom":
        return float(d.get("custom") or 1.0)
    for c in core.CANDIDATES:
        if c["key"] == d.get("speed"):
            return c["factor"]
    return 1.0


def state_to_plan(state, subfolder=None):
    """Состояние редактора (то же, что в интерфейсе) → план сборки и путь результата."""
    main_path = safe_path(state["main"]["path"])
    main_info = core.get_info(main_path)
    keep = {int(t["id"]) for t in state["main"]["tracks"] if t.get("keep", True)}
    keep_a = [t["id"] for t in main_info["tracks"] if t["type"] == "audio" and t["id"] in keep]
    keep_s = [t["id"] for t in main_info["tracks"] if t["type"] == "subtitles" and t["id"] in keep]

    donors, index_map = [], {}
    for orig_i, d in enumerate(state.get("donors", [])):
        take = d.get("take") or {}
        if not take:
            continue
        path = safe_path(d["info"]["path"])
        info = core.get_info(path)
        by_id = {t["id"]: t for t in info["tracks"]}
        tracks = []
        for tid, ov in take.items():
            src = by_id.get(int(tid))
            if not src or src["type"] not in ("audio", "subtitles"):
                continue
            tr = dict(src)
            tr["name"] = (ov.get("name") or "").strip()
            tr["lang"] = (ov.get("lang") or src["lang"] or "und").strip()
            base = int(round(float(d.get("delay") or 0)))
            tr["delay"] = base + (int(round(float(ov.get("rel") or 0))) if src["type"] == "audio" else 0)
            tracks.append(tr)
        if tracks:
            index_map[orig_i] = len(donors)
            donors.append({"path": path, "info": info, "tracks": tracks,
                           "delay": int(round(float(d.get("delay") or 0))), "factor": factor_of(d)})

    all_a = [t["id"] for t in main_info["tracks"] if t["type"] == "audio"]
    all_s = [t["id"] for t in main_info["tracks"] if t["type"] == "subtitles"]
    dflt = state.get("dflt") or "keep"
    default_audio = None
    if dflt != "keep":
        src, tid = dflt.split(":")
        if src == "main":
            default_audio = {"src": "main", "id": int(tid)}
        elif int(src) in index_map:
            default_audio = {"src": index_map[int(src)], "id": int(tid)}
    if not donors and set(keep_a) == set(all_a) and set(keep_s) == set(all_s) and not default_audio:
        raise ValueError(t("e.nochange"))

    stem = os.path.splitext(main_info["name"])[0]
    if state.get("outMode") == "output":
        folder = os.path.join(OUTPUT_DIR, subfolder) if subfolder else OUTPUT_DIR
        fallback = stem + ".mkv"
    else:
        folder = os.path.dirname(main_path)
        fallback = stem + " [+tracks].mkv"
    target = os.path.join(folder, clean_name(state.get("outName"), fallback))
    if os.path.realpath(target) == main_path:
        raise ValueError(t("e.samename"))
    check_ref = next((int(d["dstRef"]) for d in state.get("donors", [])
                      if d.get("take") and d.get("dstRef") is not None), None)
    plan = {"main": main_path, "main_info": main_info, "main_audio_keep": keep_a,
            "main_subs_keep": keep_s, "default_audio": default_audio, "donors": donors,
            "check_ref": check_ref, "order_langs": list(SETTINGS.get("order_langs") or []),
            "compat": state.get("compat") if state.get("compat") in ("off", "replace", "add")
            else SETTINGS.get("compat", "off")}
    return plan, target


# ================================================================ задачи сборки

class Jobs:
    def __init__(self):
        self.jobs = {}
        self.plans = {}
        self.cancels = {}
        self.dirty = False
        for j in load_json(JOBS_FILE, []):
            if j.get("status") in ("queued", "running"):
                j.update(status="failed", error=t("e.restart"))
            self.jobs[j["id"]] = j

    def add(self, title, plan, target, kind="manual", draft_id=None):
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "draft_id": draft_id, "title": title, "status": "queued",
               "progress": 0, "phase": t("j.queued"), "log": [], "target": target, "output": None,
               "error": None, "created": time.time(), "started": None, "finished": None,
               "summary": plan_summary(plan)}
        with lock:
            self.jobs[jid] = job
            self.plans[jid] = plan
            self.cancels[jid] = threading.Event()
            self.dirty = True
        work_queue.put(jid)
        return job

    def update(self, jid, **kw):
        with lock:
            if jid in self.jobs:
                self.jobs[jid].update(kw)
                self.dirty = True

    def log(self, jid, msg):
        with lock:
            j = self.jobs.get(jid)
            if j:
                j["log"] = (j["log"] + [time.strftime("%H:%M:%S ") + msg])[-400:]
                self.dirty = True

    def list(self):
        with lock:
            return [dict(j) for j in reversed(list(self.jobs.values()))]

    def remove(self, jid):
        with lock:
            j = self.jobs.get(jid)
            if j and j["status"] not in ("queued", "running"):
                del self.jobs[jid]
                self.plans.pop(jid, None)
                self.cancels.pop(jid, None)
                self.dirty = True
                return True
        return False

    def snapshot(self):
        with lock:
            self.dirty = False
            return list(self.jobs.values())[-200:]


def plan_summary(plan):
    add = sum(len(d["tracks"]) for d in plan["donors"])
    main_audio = sum(1 for t in plan["main_info"]["tracks"] if t["type"] == "audio")
    main_subs = sum(1 for t in plan["main_info"]["tracks"] if t["type"] == "subtitles")
    removed = (main_audio - len(plan["main_audio_keep"])) + (main_subs - len(plan["main_subs_keep"]))
    return {"add": add, "removed": removed, "donors": len(plan["donors"])}


work_queue = queue.Queue()
jobs = Jobs()
running = [0]
gate = threading.Condition()


def worker():
    while True:
        jid = work_queue.get()
        with gate:
            while running[0] >= max(1, int(SETTINGS.get("parallel", 1))):
                gate.wait(2)
            running[0] += 1
        try:
            run_one(jid)
        finally:
            with gate:
                running[0] -= 1
                gate.notify_all()


def run_one(jid):
    job = jobs.jobs.get(jid)
    if not job or job["status"] != "queued":
        return
    cancel = jobs.cancels.get(jid)
    plan = jobs.plans.get(jid)
    jobs.update(jid, status="running", started=time.time(), phase=t("j.start"))
    draft_status(job.get("draft_id"), "building")
    log = lambda m: jobs.log(jid, m)  # noqa: E731
    try:
        output = unique_path(job["target"])
        os.makedirs(os.path.dirname(output), exist_ok=True)
        jobs.update(jid, output=output)
        tmp = os.path.join(os.path.dirname(output), f".liplock-tmp-{jid}")
        output, check = core.run_job(plan, output, tmp, log,
                                     lambda p, ph: jobs.update(jid, progress=round(p, 1), phase=ph), cancel)
        jobs.update(jid, status="done", progress=100, phase=t("b.phase.done"), finished=time.time(), check=check)
        draft_finished(job.get("draft_id"), "done", output=output, check=check)
    except core.Cancelled:
        jobs.update(jid, status="cancelled", phase=t("j.cancelled"), finished=time.time())
        log(t("j.cancel.log"))
        draft_finished(job.get("draft_id"), "cancelled")
    except Exception as e:  # noqa: BLE001
        jobs.update(jid, status="failed", phase=t("j.failed"), error=str(e), finished=time.time())
        log(t("b.error", msg=e))
        if os.environ.get("DEBUG"):
            log(traceback.format_exc())
        draft_finished(job.get("draft_id"), "failed", error=str(e))
    finally:
        jobs.plans.pop(jid, None)


# ================================================================ черновики

drafts = {}
for _d in load_json(DRAFTS_FILE, []):
    drafts[_d["id"]] = _d
drafts_dirty = [False]
analysis_queue = queue.Queue()


def touch_drafts():
    drafts_dirty[0] = True


def draft_status(did, status, **kw):
    if not did:
        return
    with lock:
        d = drafts.get(did)
        if d:
            d.update(status=status, **kw)
            touch_drafts()


def write_marker(folder, data):
    try:
        with open(os.path.join(folder, MARKER), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def read_marker(folder):
    for name in (MARKER,) + OLD_MARKERS:
        data = load_json(os.path.join(folder, name), None)
        if data is not None:
            return data
    return None


def draft_finished(did, status, output=None, error=None, check=None):
    if not did:
        return
    with lock:
        d = drafts.get(did)
        if not d:
            return
        d.update(status=status, output=output, error=error, finished=time.time(), check=check)
        touch_drafts()
        folder, fsig = d.get("folder"), d.get("folder_sig") or d.get("sig")
        siblings = [x for x in drafts.values() if folder and x.get("folder") == folder]
    if not folder:
        return
    if status == "done" and SETTINGS.get("after") == "delete" and all(x["status"] == "done" for x in siblings):
        shutil.rmtree(folder, ignore_errors=True)       # a season folder goes only when every episode is built
        draft_status(did, "done", note=t("w.removed"), note_t=i18n.M("w.removed"))
    else:
        write_marker(folder, {"sig": fsig, "draft": did, "status": status})


def _media(d, exts):
    try:
        return sorted(os.path.join(d, n) for n in os.listdir(d)
                      if not n.startswith(".") and os.path.isfile(os.path.join(d, n))
                      and os.path.splitext(n)[1].lower() in exts)
    except OSError:
        return []


def plan_pairs(mains, donors):
    """[(code|None, main, [donors])], leftover donors, problem."""
    if not mains:
        return [], [], t("w.nomain")
    if not donors:
        return [], [], t("w.nodonors")
    if len(mains) == 1:
        return [(None, mains[0], donors)], [], None
    res = core.pair_episodes(mains, donors)
    if not res:
        return [], [], t("w.nopairs", n=len(mains), m=len(donors))
    pairs, leftover = res
    pairs = [p for p in pairs if p[2]]
    if not pairs:
        return [], leftover, t("w.nopairs", n=len(mains), m=len(donors))
    return pairs, leftover, None


def folder_layout(folder):
    """Returns (pairs, all_files, problem) for a watch-folder subfolder."""
    def find(names):
        for n in os.listdir(folder):
            if n.lower() in names and os.path.isdir(os.path.join(folder, n)):
                return os.path.join(folder, n)
        return None

    md, dd = find(MAIN_DIRS), find(DONOR_DIRS)
    if not md or not dd:
        return [], [], t("w.layout")
    mains, donors = _media(md, core.VIDEO_EXT), _media(dd, core.MEDIA_EXT)
    pairs, _left, problem = plan_pairs(mains, donors)
    return pairs, mains + donors, problem


def signature(files):
    h = hashlib.sha1()
    for p in files:
        try:
            st = os.stat(p)
            h.update(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode())
        except OSError:
            pass
    return h.hexdigest()


def create_draft(name, main, donors, folder=None, folder_sig=None, out_sub=None, out_mode="output", code=None):
    did = uuid.uuid4().hex[:10]
    with lock:
        drafts[did] = {"id": did, "folder": folder, "folder_sig": folder_sig, "name": name,
                       "out_sub": out_sub, "out_mode": out_mode, "code": code,
                       "sig": signature([main] + donors), "created": time.time(),
                       "status": "analyzing", "error": None, "job_id": None, "output": None,
                       "main_path": main, "donor_paths": donors, "state": None, "note": None}
        touch_drafts()
    analysis_queue.put(did)
    return did


watch_state = {}
scan_now = threading.Event()


def scan_watch():
    if not os.path.isdir(WATCH_DIR) or not SETTINGS.get("enabled"):
        return
    now = time.time()
    for name in sorted(os.listdir(WATCH_DIR)):
        folder = os.path.join(WATCH_DIR, name)
        if name.startswith(".") or not os.path.isdir(folder):
            continue
        pairs, files, problem = folder_layout(folder)
        if problem:
            watch_state.pop(folder, None)
            continue
        sig = signature(files)
        with lock:
            existing = [d for d in drafts.values() if d.get("folder") == folder]
        if existing and all((d.get("folder_sig") or d.get("sig")) == sig for d in existing):
            continue
        marker = read_marker(folder)
        if not existing and marker and marker.get("sig") == sig:
            continue
        st = watch_state.get(folder)
        if not st or st["sig"] != sig:
            watch_state[folder] = {"sig": sig, "since": now}
            continue
        if now - st["since"] < STABLE_SECONDS:
            continue
        if any(d["status"] in ("queued", "building") for d in existing):
            continue
        with lock:
            for d in existing:
                drafts.pop(d["id"], None)
        for code, main, donors in pairs:
            create_draft(f"{name} · {code}" if code else name, main, donors,
                         folder=folder, folder_sig=sig, out_sub=name, code=code)
        write_marker(folder, {"sig": sig, "status": "draft"})


def folders_overview():
    out = []
    if not os.path.isdir(WATCH_DIR):
        return out
    with lock:
        by_folder = {}
        for d in drafts.values():
            if d.get("folder"):
                by_folder.setdefault(d["folder"], []).append(d)
    now = time.time()
    for name in sorted(os.listdir(WATCH_DIR)):
        folder = os.path.join(WATCH_DIR, name)
        if name.startswith(".") or not os.path.isdir(folder):
            continue
        pairs, files, problem = folder_layout(folder)
        if problem:
            out.append({"name": name, "state": problem, "problem": True})
            continue
        sig = signature(files)
        ds = by_folder.get(folder) or []
        if ds and all((d.get("folder_sig") or d.get("sig")) == sig for d in ds):
            continue                                   # shown as drafts
        marker = read_marker(folder)
        if marker and marker.get("sig") == sig:
            out.append({"name": name, "state": t("w.done"), "problem": False, "can_reset": True})
            continue
        st = watch_state.get(folder)
        left = STABLE_SECONDS - (now - st["since"]) if st and st["sig"] == sig else STABLE_SECONDS
        state = t("w.off") if not SETTINGS.get("enabled") else t("w.wait", s=max(0, int(left)) + POLL_SECONDS)
        if len(pairs) > 1:
            state = t("w.episodes", n=len(pairs)) + " " + state
        out.append({"name": name, "state": state, "problem": False})
    return out


def build_draft_state(did):
    with lock:
        d = drafts.get(did)
        if not d:
            return
        st = dict(SETTINGS)
        main_path, donor_paths = d["main_path"], d["donor_paths"]
    notes = []
    main = core.get_info(main_path)
    remove = st.get("remove_langs") or []
    for t in main["tracks"]:
        t["keep"] = t["type"] == "video" or t["lang"] not in remove
    existing = {core.track_key(t) for t in main["tracks"] if t["keep"] and t["type"] in ("audio", "subtitles")}
    main_audio = [t for t in main["tracks"] if t["type"] == "audio"]
    prio = st.get("langs") or ["rus", "eng"]
    ok_all, taken_any, dflt = True, False, "keep"

    donors = []
    for i, p in enumerate(donor_paths):
        try:
            info = core.get_info(p)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{os.path.basename(p)}: {e}")
            ok_all = False
            continue
        take_list = core.suggest_take(info, existing, st)
        existing.update(core.track_key(t) for t in take_list)
        taken_any = taken_any or bool(take_list)
        dd = {"info": info, "take": {str(t["id"]): {"name": t["name"], "lang": t["lang"]} for t in take_list},
              "srcRef": None, "dstRef": None, "result": None, "delay": 0, "speed": "same", "custom": 1,
              "raw": False}
        d_audio = [t for t in info["tracks"] if t["type"] == "audio"]
        if d_audio and main_audio:
            dd["srcRef"], dd["dstRef"] = core.choose_ref(d_audio, main_audio, prio)
            if take_list:
                lines = []
                res = core.analyze(p, dd["srcRef"], main_path, dd["dstRef"],
                                   info["duration"] or main["duration"], lines.append,
                                   dst_dur=main["duration"])
                dd["result"] = res
                if res["delay"] is not None:
                    dd["delay"] = res["delay"]
                dd["speed"] = res["speed"]
                if res["speed"] == "custom":
                    dd["custom"] = res["factor"]
                ok_all = ok_all and res["ok"]
                if res["ok"]:
                    rel = core.relative_offsets(p, dd["srcRef"], [t["id"] for t in d_audio],
                                                info["duration"] or main["duration"],
                                                names={t["id"]: core.track_label(t) for t in d_audio})
                    res["rel"] = rel
                    for tid, v in dd["take"].items():
                        v["rel"] = rel.get(tid, {}).get("rel", 0)
                if not res["ok"]:
                    notes.append(i18n.join([info["name"], i18n.lower(res.get("label_t") or res["label"])], ": "))
        elif take_list:
            ok_all = False
            notes.append(i18n.M("d.noaudio", name=info["name"]))
        if st.get("default_lang") and dflt == "keep":
            hit = next((t for t in take_list if t["type"] == "audio" and t["lang"] == st["default_lang"]), None)
            if hit:
                dflt = f"{len(donors)}:{hit['id']}"
        donors.append(dd)

    if not taken_any:
        notes.append(i18n.M("d.nothing"))
        ok_all = False
    with lock:
        out_mode = (drafts.get(did) or {}).get("out_mode") or "output"
    state = {"main": main, "donors": donors, "dflt": dflt, "outMode": out_mode, "outName": "",
             "compat": SETTINGS.get("compat", "off")}
    status = "ready" if ok_all else "attention"
    with lock:
        if did in drafts:
            drafts[did].update(state=state, status=status, error=None)
            i18n.say(drafts[did], "note", i18n.join(notes, "; ") if notes else None)
            touch_drafts()
    if status == "ready" and st.get("auto_build"):
        try:
            queue_draft(did)
        except Exception as e:  # noqa: BLE001
            draft_status(did, "attention", note=str(e), note_t=None)


def analysis_worker():
    while True:
        did = analysis_queue.get()
        try:
            draft_status(did, "analyzing")
            build_draft_state(did)
        except Exception as e:  # noqa: BLE001
            draft_status(did, "error", error=str(e))


def queue_draft(did, state=None):
    with lock:
        d = drafts.get(did)
        if not d:
            raise ValueError(t("e.nodraft"))
        if d["status"] in ("queued", "building"):
            raise ValueError(t("e.inqueue"))
        if state is not None:
            d["state"] = state
        if not d.get("state"):
            raise ValueError(t("e.analyzing"))
        st, name, sub = d["state"], d["name"], d.get("out_sub") or d["name"]
    plan, target = state_to_plan(st, subfolder=sub)
    job = jobs.add(name, plan, target, kind="draft", draft_id=did)
    draft_status(did, "queued", job_id=job["id"], error=None, output=None)
    return job


def watcher():
    while True:
        try:
            scan_watch()
        except Exception as e:  # noqa: BLE001
            print("watch error:", e, flush=True)
        scan_now.wait(POLL_SECONDS)
        scan_now.clear()


def saver():
    while True:
        time.sleep(2)
        if jobs.dirty:
            save_json(JOBS_FILE, jobs.snapshot())
        if drafts_dirty[0]:
            with lock:
                drafts_dirty[0] = False
                data = list(drafts.values())
            save_json(DRAFTS_FILE, data)


def restore_after_restart():
    for did, d in list(drafts.items()):
        if d["status"] == "analyzing":
            analysis_queue.put(did)
        elif d["status"] in ("queued", "building"):
            d["status"] = "ready"
            try:
                queue_draft(did)
            except Exception as e:  # noqa: BLE001
                d.update(status="attention", note=t("e.requeue", err=e), note_t=i18n.M("e.requeue", err=str(e)))
    touch_drafts()


# ================================================================ API

BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # PyInstaller bundle or source
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 400)
    if not isinstance(code, int):
        code = 400
    return jsonify({"error": str(getattr(e, "description", None) or e)}), code


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.after_request
def _localize_json(resp):
    """Stored verdicts and notes are re-rendered in the current interface language."""
    if resp.is_json and request.path.startswith("/api/") and resp.status_code < 400:
        try:
            data = resp.get_json()
            if "_t" in resp.get_data(as_text=True):
                resp.set_data(json.dumps(i18n.localize(data), ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
    return resp


@app.post("/api/localize")
def api_localize():
    """The editor sends its state after RU/EN is switched and gets the texts in the new language."""
    return jsonify(request.get_json(force=True))


@app.get("/api/config")
def api_config():
    return jsonify({"roots": roots(), "output_dir": OUTPUT_DIR, "watch_dir": WATCH_DIR,
                    "speeds": [{"key": c["key"], "factor": c["factor"], "label": core.speed_label(c["key"])}
                               for c in core.CANDIDATES],
                    "lang": i18n.get_lang(), "compat": SETTINGS.get("compat", "off"),
                    "reencode_above": core.REENCODE_ABOVE, "poll": POLL_SECONDS, "stable": STABLE_SECONDS})


@app.get("/api/browse")
def api_browse():
    path = safe_path(request.args.get("path") or MEDIA_DIRS[0])
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    dirs, files = [], []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": os.path.join(path, e.name)})
                elif os.path.splitext(e.name)[1].lower() in core.MEDIA_EXT:
                    files.append({"name": e.name, "size": e.stat().st_size, "path": os.path.join(path, e.name)})
            except OSError:
                pass
    parent = os.path.dirname(path)
    try:
        safe_path(parent)
    except ValueError:
        parent = None
    if parent == path:
        parent = None
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    return jsonify({"path": path, "parent": parent, "dirs": dirs, "files": files})


@app.get("/api/info")
def api_info():
    return jsonify(core.get_info(safe_path(request.args.get("path"))))


@app.post("/api/analyze")
def api_analyze():
    b = request.get_json(force=True)
    main, donor = safe_path(b.get("main")), safe_path(b.get("donor"))
    dinfo = core.get_info(donor)
    lines = []
    main_dur = core.get_info(main)["duration"]
    dur = dinfo["duration"] or main_dur
    res = core.analyze(donor, int(b["src_ref"]), main, int(b["dst_ref"]), dur, lines.append, dst_dur=main_dur)
    if res["ok"]:
        d_audio = [t for t in dinfo["tracks"] if t["type"] == "audio"]
        res["rel"] = core.relative_offsets(donor, int(b["src_ref"]), [t["id"] for t in d_audio], dur,
                                           lines.append, names={t["id"]: core.track_label(t) for t in d_audio})
    res["log"] = lines
    return jsonify(res)


@app.post("/api/jobs")
def api_create_job():
    state = request.get_json(force=True)["state"]
    plan, target = state_to_plan(state)
    return jsonify(jobs.add(plan["main_info"]["name"], plan, target))


@app.get("/api/jobs")
def api_jobs():
    return jsonify(jobs.list())


@app.post("/api/jobs/<jid>/cancel")
def api_cancel(jid):
    j = jobs.jobs.get(jid)
    if not j:
        raise ValueError(t("e.nojob"))
    if j["status"] == "queued":
        jobs.update(jid, status="cancelled", phase=t("j.cancelled"), finished=time.time())
        draft_finished(j.get("draft_id"), "cancelled")
    elif j["status"] == "running":
        jobs.cancels[jid].set()
    return jsonify({"ok": True})


@app.delete("/api/jobs/<jid>")
def api_delete_job(jid):
    return jsonify({"ok": jobs.remove(jid)})


@app.post("/api/jobs/clear")
def api_clear_jobs():
    n = sum(jobs.remove(j["id"]) for j in jobs.list())
    return jsonify({"removed": n})


@app.get("/api/drafts")
def api_drafts():
    with lock:
        items = sorted(drafts.values(), key=lambda d: d["created"], reverse=True)
        items = [dict(d) for d in items]
    for d in items:
        jid = d.get("job_id")
        j = jobs.jobs.get(jid) if jid else None
        d["job"] = {"progress": j["progress"], "phase": j["phase"]} if j else None
    return jsonify({"drafts": items, "folders": folders_overview(), "watch_dir": WATCH_DIR,
                    "enabled": SETTINGS.get("enabled")})


@app.put("/api/drafts/<did>")
def api_save_draft(did):
    state = request.get_json(force=True)["state"]
    with lock:
        d = drafts.get(did)
        if not d:
            raise ValueError(t("e.nodraft"))
        d["state"] = state
        if d["status"] in ("done", "failed", "cancelled", "attention", "ready", "error"):
            d["status"] = "ready"
        touch_drafts()
    return jsonify({"ok": True})


@app.post("/api/drafts/<did>/queue")
def api_queue_draft(did):
    b = request.get_json(silent=True) or {}
    return jsonify(queue_draft(did, b.get("state")))


@app.post("/api/drafts/queue")
def api_queue_many():
    ids = request.get_json(force=True).get("ids", [])
    done, errors = 0, []
    for did in ids:
        try:
            queue_draft(did)
            done += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{drafts.get(did, {}).get('name', did)}: {e}")
    return jsonify({"queued": done, "errors": errors})


@app.post("/api/drafts/<did>/reanalyze")
def api_reanalyze(did):
    with lock:
        d = drafts.get(did)
        if not d or d["status"] in ("queued", "building"):
            raise ValueError(t("e.notnow"))
        missing = [p for p in [d["main_path"]] + d["donor_paths"] if not os.path.exists(p)]
        if missing:
            raise ValueError(t("e.notfound", path=os.path.basename(missing[0])))
        d.update(sig=signature([d["main_path"]] + d["donor_paths"]), status="analyzing", note=None, note_t=None, error=None)
        touch_drafts()
    analysis_queue.put(did)
    return jsonify({"ok": True})


@app.delete("/api/drafts/<did>")
def api_delete_draft(did):
    with lock:
        d = drafts.get(did)
        if not d or d["status"] in ("queued", "building"):
            raise ValueError(t("e.draftqueued"))
        drafts.pop(did)
        touch_drafts()
        left = [x for x in drafts.values() if d.get("folder") and x.get("folder") == d["folder"]]
    if d.get("folder") and os.path.isdir(d["folder"]) and not left:
        write_marker(d["folder"], {"sig": d.get("folder_sig") or d["sig"], "draft": did, "status": "dismissed"})
    return jsonify({"ok": True})


@app.post("/api/watch/reset")
def api_watch_reset():
    name = os.path.basename(request.get_json(force=True).get("name", ""))
    folder = safe_path(os.path.join(WATCH_DIR, name))
    for m in (MARKER,) + OLD_MARKERS:
        try:
            os.remove(os.path.join(folder, m))
        except OSError:
            pass
    _pairs, files, problem = folder_layout(folder)
    if not problem:   # files are already in place — no need to wait for them to settle
        watch_state[folder] = {"sig": signature(files), "since": 0}
    scan_now.set()
    return jsonify({"ok": True})


@app.post("/api/batch")
def api_batch():
    """Season from folders: pairs main files with donor files by episode number, one draft each."""
    b = request.get_json(force=True)
    main_dir = safe_path(b.get("main_dir"))
    donor_dirs = [safe_path(x) for x in b.get("donor_dirs") or []]
    if not os.path.isdir(main_dir) or not donor_dirs:
        raise ValueError(t("e.batchdirs"))
    mains = _media(main_dir, core.VIDEO_EXT)
    donors = [f for dd in donor_dirs for f in _media(dd, core.MEDIA_EXT) if f not in mains]
    pairs, leftover, problem = plan_pairs(mains, donors)
    if problem:
        raise ValueError(problem)
    base = os.path.basename(main_dir.rstrip("/\\"))
    if base.lower() in MAIN_DIRS:                    # ".../Show S01/main" — name it after the season folder
        base = os.path.basename(os.path.dirname(main_dir.rstrip("/\\"))) or base
    name = clean_name(b.get("name") or base, "batch")[:-4]
    out_mode = "output" if b.get("out_mode") == "output" else "beside"
    for code, main, ds in pairs:
        create_draft(f"{name} · {code}" if code else name, main, ds, out_sub=name, out_mode=out_mode, code=code)
    paired = {m for _c, m, _d in pairs}
    return jsonify({"created": len(pairs), "unpaired": [os.path.basename(m) for m in mains if m not in paired],
                    "leftover": [os.path.basename(x) for x in leftover]})


@app.post("/api/watch/scan")
def api_scan():
    scan_now.set()
    return jsonify({"ok": True})


@app.get("/api/settings")
def api_get_settings():
    return jsonify(SETTINGS)


@app.post("/api/settings")
def api_settings():
    b = request.get_json(force=True)
    with lock:
        for k in ("enabled", "take_subs", "auto_build"):
            if k in b:
                SETTINGS[k] = bool(b[k])
        if b.get("mode") in ("new", "all"):
            SETTINGS["mode"] = b["mode"]
        if b.get("after") in ("keep", "delete"):
            SETTINGS["after"] = b["after"]
        if b.get("compat") in ("off", "replace", "add"):
            SETTINGS["compat"] = b["compat"]
        for k in ("langs", "remove_langs"):
            if k in b:
                v = b[k].split(",") if isinstance(b[k], str) else b[k]
                SETTINGS[k] = [x.strip().lower() for x in v if x.strip()]
        if "default_lang" in b:
            SETTINGS["default_lang"] = (b["default_lang"] or "").strip().lower()
        if "parallel" in b:
            SETTINGS["parallel"] = max(1, min(4, int(b["parallel"])))
        if "order_langs" in b:
            v = b["order_langs"].split(",") if isinstance(b["order_langs"], str) else b["order_langs"]
            SETTINGS["order_langs"] = [x.strip().lower() for x in v if x.strip()]
        if b.get("lang") in ("ru", "en"):
            SETTINGS["lang"] = b["lang"]
            i18n.set_lang(b["lang"])
        save_json(SETTINGS_FILE, SETTINGS)
        out = dict(SETTINGS)
    with gate:
        gate.notify_all()
    scan_now.set()
    return jsonify(out)


# ================================================================ запуск

def drop_privileges():
    """Like other Unraid containers: run as PUID:PGID (nobody:users by default)."""
    def num(name, default, base=10):                         # an empty field in the template means default
        try:
            return int(os.environ.get(name, "").strip() or default, base)
        except ValueError:
            return int(default, base)

    os.umask(num("UMASK", "000", 8))
    if not hasattr(os, "getuid") or os.getuid() != 0:      # Windows or already non-root
        return
    uid, gid = num("PUID", "99"), num("PGID", "100")
    for d in (CONFIG_DIR, OUTPUT_DIR, WATCH_DIR):
        try:
            os.makedirs(d, exist_ok=True)
            if os.stat(d).st_uid == 0:
                os.chown(d, uid, gid)
        except OSError:
            pass
    for f in (SETTINGS_FILE, JOBS_FILE, DRAFTS_FILE):
        try:
            os.chown(f, uid, gid)
        except OSError:
            pass
    if uid != 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)


def main():
    drop_privileges()
    for d in (OUTPUT_DIR, WATCH_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    for _ in range(4):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=analysis_worker, daemon=True).start()
    threading.Thread(target=saver, daemon=True).start()
    restore_after_restart()
    threading.Thread(target=watcher, daemon=True).start()
    print(f"LipLock: http://{HOST}:{PORT}  media={','.join(MEDIA_DIRS)} watch={WATCH_DIR} "
          f"output={OUTPUT_DIR}", flush=True)
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=12)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
