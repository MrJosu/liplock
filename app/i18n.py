"""
Переводы сообщений сервера и анализа / Server-side message catalogue.

t("key", **params) возвращает строку на текущем языке (ru | en).
"""
import os

_lang = [os.environ.get("UI_LANG", "ru").strip().lower()[:2] or "ru"]


def set_lang(lang):
    _lang[0] = "ru" if (lang or "").lower().startswith("ru") else "en"


def get_lang():
    return _lang[0]


MSG = {
    # ---- скорость / frame rate
    "speed.same": ("Одинаковая частота", "Same frame rate"),
    "speed.24>23.976": ("Донор 24 → основной 23.976", "Donor 24 → main 23.976"),
    "speed.23.976>24": ("Донор 23.976 → основной 24", "Donor 23.976 → main 24"),
    "speed.25>23.976": ("Донор 25 (PAL) → основной 23.976", "Donor 25 (PAL) → main 23.976"),
    "speed.23.976>25": ("Донор 23.976 → основной 25", "Donor 23.976 → main 25"),
    "speed.25>24": ("Донор 25 → основной 24", "Donor 25 → main 24"),
    "speed.24>25": ("Донор 24 → основной 25", "Donor 24 → main 25"),
    "speed.custom": ("Нестандартная скорость ×{f:.6f}", "Non-standard speed ×{f:.6f}"),

    # ---- анализ / analysis
    "an.start": ("Определяю скорость и сдвиг…", "Detecting speed and offset…"),
    "an.speed": ("Скорость: {label}", "Speed: {label}"),
    "an.nomatch.label": ("Звук не совпал", "Audio did not match"),
    "an.nomatch.msg": ("Возможно, это разные дубляжи или сдвиг больше {win} с. Укажи сдвиг вручную "
                       "или выбери другую пару дорожек.",
                       "These may be different dubs, or the offset is larger than {win} s. Enter the offset "
                       "manually or pick another pair of tracks."),
    "an.scan.more": ("В начале звук не нашёлся — ищу дальше по фильму…",
                     "No match near the start — searching further into the film…"),
    "an.recheck": ("Замер на {a} не совпал, перепроверил на {b} — сходится.",
                   "Measurement at {a} was off; rechecked at {b} — it matches."),
    "an.recheck1": (" Один замер был ложным и перепроверен рядом.",
                    " One measurement was a false match and was rechecked nearby."),
    "an.recheckN": (" Ложных замеров перепроверено: {n}.", " False matches rechecked nearby: {n}."),
    "an.few": ("Мало надёжных точек — проверь синхронизацию вручную.",
               "Too few reliable points — check the sync manually."),
    "an.stable": ("Сдвиг стабилен по всему фильму (разброс {spread} мс).{note}",
                  "Offset is stable across the whole film (spread {spread} ms).{note}"),
    "an.out.first": ("Первая точка ({where}) не совпала, остальные до конца фильма сходятся. "
                     "Возможно, начало дорожки собрано иначе — глянь первые минуты.",
                     "The first point ({where}) did not match; all the others agree. The start of the "
                     "track may be assembled differently — check the first minutes."),
    "an.out.last": ("Последняя точка ({where}) не совпала, остальные сходятся. "
                    "Возможно, отличаются финальные сцены или титры — глянь конец.",
                    "The last point ({where}) did not match; the others agree. The final scenes or "
                    "credits may differ — check the ending."),
    "an.out.mid": ("Одна точка ({where}) не совпала, но точки до и после сходятся — скорее всего, "
                   "ложное совпадение. Стоит глянуть это место.",
                   "One point ({where}) did not match, but the points before and after agree — most "
                   "likely a false match. Worth a quick look."),
    "an.drift": ("Найден плавный дрейф с нестандартным коэффициентом.",
                 "Found a smooth drift with a non-standard factor."),
    "an.inconsistent": ("Сдвиг скачет между точками — похоже, в версиях разный монтаж. "
                        "Одна поправка подойдёт лишь для части фильма.",
                        "The offset jumps between points — the cuts look different. A single correction "
                        "will only fit part of the film."),
    "an.missed": ("В {missed} точках из {total} звук не нашёлся рядом с ожидаемым местом. "
                  "Сверь по другой паре дорожек (лучше оригинал) или проверь вручную.",
                  "At {missed} of {total} points the audio was not found near the expected place. "
                  "Compare another pair of tracks (the original is best) or check manually."),
    "an.result": ("Итог: сдвиг {delay:+d} мс, скорость ×{f:.6f} — {msg}",
                  "Result: offset {delay:+d} ms, speed ×{f:.6f} — {msg}"),
    "an.rel": ("Дорожка {name} в доноре {dir} на {ms} мс относительно опорной — учту отдельно.",
               "Track {name} in the donor is {dir} by {ms} ms relative to the reference — will correct it."),
    "an.rel.late": ("отстаёт", "late"),
    "an.rel.early": ("опережает", "early"),
    "min": ("{m} мин", "{m} min"),

    # ---- сборка / build
    "b.reencode": ("Перекодирую дорожку [{id}] из {file}", "Re-encoding track [{id}] from {file}"),
    "b.reencode.why": (" (исправление скорости → E-AC3)", " (speed correction → E-AC3)"),
    "b.name.fixed": ("{name} (E-AC3, скорость исправлена)", "{name} (E-AC3, speed corrected)"),
    "b.name.fixed2": ("E-AC3 {ch}ch (из {codec}, скорость исправлена)",
                      "E-AC3 {ch}ch (from {codec}, speed corrected)"),
    "b.compat.why": (" (для совместимости → E-AC3)", " (for compatibility → E-AC3)"),
    "b.name.compat": ("{name} (E-AC3)", "{name} (E-AC3)"),
    "b.name.compat2": ("E-AC3 {ch}ch (из {codec})", "E-AC3 {ch}ch (from {codec})"),
    "b.muxing": ("Собираю файл: {name}", "Muxing: {name}"),
    "b.phase.mux": ("Собираю MKV", "Muxing MKV"),
    "b.warning": ("Предупреждение: {msg}", "Warning: {msg}"),
    "b.error": ("Ошибка: {msg}", "Error: {msg}"),
    "b.done": ("Готово: {path}", "Done: {path}"),
    "b.phase.check": ("Проверяю готовый файл", "Checking the result"),
    "b.phase.done": ("Готово", "Done"),
    "b.check.failed": ("Проверка не выполнилась: {err}", "Check could not run: {err}"),
    "b.order": ("Порядок дорожек: {order}", "Track order: {order}"),

    # ---- проверка / post-check
    "c.count": ("В файле {got} аудиодорожек, а ожидалось {want}.",
                "The file has {got} audio tracks, expected {want}."),
    "c.nothing": ("Новых аудиодорожек нет — проверять нечего.", "No new audio tracks — nothing to check."),
    "c.noref": ("В основном файле не осталось звука для сравнения.",
                "No audio left in the main file to compare with."),
    "c.ok": ("в синхроне с «{ref}» ({n} из {of} точек, {ms:+.0f} мс)",
             "in sync with “{ref}” ({n} of {of} points, {ms:+.0f} ms)"),
    "c.late": ("относительно «{ref}» звук опаздывает на {ms:.0f} мс", "is {ms:.0f} ms late vs “{ref}”"),
    "c.early": ("относительно «{ref}» звук спешит на {ms:.0f} мс", "is {ms:.0f} ms early vs “{ref}”"),
    "c.unstable": ("сдвиг непостоянный ({n} из {of} точек сходятся)",
                   "offset is unstable ({n} of {of} points agree)"),
    "c.unknown": ("не удалось проверить — звук слишком отличается от оригинала",
                  "could not check — audio differs too much from the original"),
    "c.flag": ("; флаг «по умолчанию» не выставился", "; the default flag was not set"),
    "c.log": ("Проверка: «{name}» — {text}", "Check: “{name}” — {text}"),
    "c.log2": ("Проверка: {msg}", "Check: {msg}"),
    "c.bad": ("Проверка нашла проблемы: {bad} из {total}.", "The check found problems: {bad} of {total}."),
    "c.partial": ("Часть дорожек проверить не удалось.", "Some tracks could not be checked."),
    "c.allok": ("Все новые дорожки в синхроне.", "All new tracks are in sync."),

    # ---- видео / video
    "v.bits": ("{n} бит", "{n}-bit"),
    "v.dv": ("Dolby Vision профиль {n}", "Dolby Vision profile {n}"),
    "v.same": ("Видео не изменилось: {desc}.", "Video unchanged: {desc}."),
    "v.diff": ("Видео отличается от основного: было {a}, стало {b}.",
               "Video differs from the main file: was {a}, now {b}."),
    "v.unknown": ("Не удалось прочитать параметры видео.", "Could not read the video parameters."),

    # ---- ошибки и служебное / errors
    "e.format": ("Формат не распознан: {name}", "Unrecognised format: {name}"),
    "e.nopath": ("Не указан путь", "No path given"),
    "e.notfound": ("Файл не найден: {path}", "File not found: {path}"),
    "e.outside": ("Путь вне разрешённых папок", "Path is outside the allowed folders"),
    "e.nochange": ("Ничего не меняется: отметь дорожки, которые нужно добавить или убрать.",
                   "Nothing changes: tick the tracks to add or remove."),
    "e.samename": ("Имя результата совпадает с основным файлом.", "The output name equals the main file."),
    "e.nojob": ("Нет такой задачи", "No such job"),
    "e.nodraft": ("Нет такого черновика", "No such draft"),
    "e.inqueue": ("Уже в очереди", "Already queued"),
    "e.analyzing": ("Черновик ещё анализируется", "The draft is still being analysed"),
    "e.notnow": ("Сейчас нельзя", "Not possible right now"),
    "e.draftqueued": ("Черновик в очереди — сначала отмени сборку",
                      "The draft is queued — cancel the build first"),
    "e.restart": ("Прервано перезапуском контейнера", "Interrupted by a container restart"),
    "e.requeue": ("Не удалось вернуть в очередь: {err}", "Could not requeue: {err}"),

    "j.queued": ("В очереди", "Queued"),
    "j.start": ("Запуск", "Starting"),
    "j.cancelled": ("Отменено", "Cancelled"),
    "j.cancel.log": ("Сборка отменена.", "Build cancelled."),
    "j.failed": ("Ошибка", "Error"),

    # ---- папка-автомат / watch folder
    "w.layout": ("Нужны подпапки main и donors", "Needs main and donors subfolders"),
    "w.nomain": ("В main нет видеофайла", "No video file in main"),
    "w.manymain": ("В main {n} файла, нужен один", "{n} files in main, expected one"),
    "w.nodonors": ("В donors пусто", "donors is empty"),
    "w.done": ("Уже обработана или черновик удалён", "Already processed or draft deleted"),
    "w.off": ("Автомат выключен", "Watching is off"),
    "w.wait": ("Жду, пока файлы перестанут меняться (~{s} с)", "Waiting for files to settle (~{s} s)"),
    "w.nopairs": ("Не удалось сопоставить серии: в main {n}, в donors {m}. Нужны номера в именах (S01E02) "
                  "или одинаковое число файлов.",
                  "Could not pair episodes: {n} in main, {m} in donors. File names need numbers (S01E02) "
                  "or both folders must hold the same number of files."),
    "w.episodes": ("Серий: {n}.", "Episodes: {n}."),
    "e.batchdirs": ("Выбери папку основных файлов и хотя бы одну папку доноров.",
                    "Pick the main folder and at least one donor folder."),
    "w.removed": ("Исходная папка удалена", "Source folder removed"),
    "d.noaudio": ("{name}: звука для сверки нет — проверь сдвиг вручную",
                  "{name}: no audio to compare — check the offset manually"),
    "d.nothing": ("По правилам брать нечего — отметь дорожки вручную",
                  "Nothing to take by the rules — tick tracks manually"),
}


def t(key, **kw):
    pair = MSG.get(key)
    if not pair:
        return key
    s = pair[0] if _lang[0] == "ru" else pair[1]
    return s.format(**kw) if kw else s


# ---------------------------------------------------------------- stored messages
# Texts that are saved (analysis verdicts, post-check results, draft notes) keep their key and
# parameters next to the text: field "message" goes with "message_t" = ["key", {params}].
# localize() renders them again in the current language, so switching RU/EN changes old results too.

def M(key, **kw):
    """A message that can be rendered later in any language."""
    return [key, kw]


def join(parts, sep=""):
    return ["+", {"parts": list(parts), "sep": sep}]


def lower(spec):
    return ["lower", {"s": spec}]


def _is_spec(x):
    return isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and isinstance(x[1], dict)


def render(spec):
    if not _is_spec(spec):
        return spec
    key, kw = spec
    if key == "+":
        return kw.get("sep", "").join(str(render(p)) for p in kw.get("parts", []) if render(p) not in (None, ""))
    if key == "lower":
        return str(render(kw.get("s"))).lower()
    return t(key, **{k: render(v) for k, v in kw.items()})


def say(d, field, spec):
    """d[field] = text in the current language, d[field + "_t"] = how to render it again."""
    d[field] = render(spec) if spec is not None else None
    d[field + "_t"] = spec
    return d


def localize(obj):
    """Re-render every stored message in obj (in place) in the current language."""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k.endswith("_t") and _is_spec(obj[k]):
                obj[k[:-2]] = render(obj[k])
            else:
                localize(obj[k])
    elif isinstance(obj, list):
        for x in obj:
            localize(x)
    return obj
