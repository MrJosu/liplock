"""
LipLock for Windows (and any desktop): starts the server on this computer only
and opens the interface in the browser. Close this window to stop LipLock.
"""
import os
import string
import sys
import threading
import time
import webbrowser


def _drives():
    if os.name != "nt":
        return [os.path.expanduser("~")]
    return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


def _defaults():
    home = os.path.expanduser("~")
    base = os.environ.get("APPDATA") or os.path.join(home, ".config")
    videos = os.path.join(home, "Videos")
    os.environ.setdefault("CONFIG_DIR", os.path.join(base, "LipLock"))
    os.environ.setdefault("WATCH_DIR", os.path.join(videos, "LipLock", "input"))
    os.environ.setdefault("OUTPUT_DIR", os.path.join(videos, "LipLock", "output"))
    os.environ.setdefault("MEDIA_DIRS", ",".join(_drives()))
    os.environ.setdefault("HOST", "127.0.0.1")          # only this computer can open it
    os.environ.setdefault("PORT", "47650")
    os.environ.setdefault("POLL_SECONDS", "10")
    os.environ.setdefault("STABLE_SECONDS", "30")


def main():
    for stream in (sys.stdout, sys.stderr):          # Windows console is not UTF-8 by default
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    _defaults()
    url = f"http://127.0.0.1:{os.environ['PORT']}/"
    ru = os.environ.get("UI_LANG", "ru").startswith("ru")
    print("LipLock", url)
    print("Не закрывай это окно, пока работаешь с LipLock." if ru else
          "Keep this window open while you use LipLock.")
    print(("Папка-автомат: " if ru else "Watch folder: ") + os.environ["WATCH_DIR"])

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    if "--no-browser" not in sys.argv:
        threading.Thread(target=open_browser, daemon=True).start()

    import server       # noqa: E402 — env must be set before import
    server.main()


if __name__ == "__main__":
    main()
