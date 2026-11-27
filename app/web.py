"""HTTP-Routen. Das HTML kommt aus pages.py und stats.py."""

from __future__ import annotations

import asyncio
import contextvars
import csv
import io
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import web

from . import archive
from . import bulkpay as bp
from . import extras as zk
from . import models as mdl
from . import orders as od
from . import plates as pl
from . import storage as lg
from .costing import compute, parse_materials, recompute_all
from .db import Store
from .defects import DEFECTS
from .export import bon_png, collect, file_name, report_pdf
from .i18n import LANG, LANGS, is_en, resolve, t
from .media import save_logo, clean_local_path, fetch_title, shrink_photo, title_from_url
from .pages import (
    FILTER_KEYS,
    VIEW_KEYS,
    DETAIL_TABS,
    _per_piece,
    TODO_OPTIONS,
    amount_due,
    db_filters,
    csv_rows,
    page_customers,
    page_job,
    page_jobs,
    page_import,
    page_reset,
    page_settings,
    page_trash,
    results,
)
from .customer import page_customer
from .stats import page_calendar, page_stats
from .ui import HINTS, STATIC_DIR, THEME, THEMES, parse_num, printer_names

log = logging.getLogger("printlog.web")

IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
MAX_PHOTO_BYTES = 20 * 1024 * 1024
TOOLS_DIR = Path(__file__).parent / "tools"
TOOLS = ("printlog-open.ps1", "printlog-open-entfernen.reg", "printlog-einrichten.cmd")

# Einrichtung per Doppelklick: Die .cmd trägt das PowerShell-Skript hinter einer Marke in sich,
# schreibt es nach %LOCALAPPDATA%\printlog und startet es dort mit ExecutionPolicy Bypass.
SETUP_CMD = r"""@echo off
setlocal
rem PrintLog: Ordner-Links einrichten (Link-Protokoll printlog-open, nur fuer deinen Benutzer).
set "PL_SELF=%~f0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:PL_SELF,[Text.Encoding]::UTF8); $i=$t.IndexOf('#PRINTLOG'+'-PS1#'); if($i -lt 0){exit 1}; $d=Join-Path $env:LOCALAPPDATA 'printlog'; New-Item -ItemType Directory -Force -Path $d | Out-Null; $f=Join-Path $d 'printlog-open.ps1'; [IO.File]::WriteAllText($f,$t.Substring($i),(New-Object Text.UTF8Encoding $true)); & $f"
if errorlevel 1 pause
exit /b
#PRINTLOG-PS1#
"""


def _filters(request: web.Request) -> dict[str, str | None]:
    f: dict[str, str | None] = {k: (request.query.get(k) or None) for k in FILTER_KEYS}
    if f["customer"] and f["customer"] != "none" and not f["customer"].isdigit():
        f["customer"] = None
    if f["model"] and not f["model"].isdigit():
        f["model"] = None
    if f["loc"] and f["loc"] != "none" and not f["loc"].isdigit():
        f["loc"] = None
    if f["day"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["day"]):
        f["day"] = None
    if f["todo"] not in TODO_OPTIONS:
        f["todo"] = None
    if "group" not in request.query:
        # Gruppierung nach Tagen merkt sich der Browser (Cookie vom Schalter auf der Seite)
        f["group"] = "day" if request.cookies.get("pl_group") == "day" else None
    elif f["group"] != "day":
        f["group"] = None
    return f


def _db_filters(f: dict[str, str | None]) -> dict[str, str | None]:
    return db_filters(f)


def _day_ts(day: str) -> float | None:
    """Datum aus einem Formular (JJJJ-MM-TT) als Zeitpunkt; heute = jetzt, sonst 12 Uhr."""
    day = day.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    d = datetime.strptime(day, "%Y-%m-%d")
    now = datetime.now()
    return (now if d.date() == now.date() else d.replace(hour=12)).timestamp()


def trash_days(store: Store) -> int:
    v = store.settings().get("trash_days")
    return int(v) if v and v.isdigit() else 30


def purge_expired(store: Store, image_dir: Path) -> int:
    """Drucke, die länger als die eingestellten Tage im Papierkorb liegen, endgültig löschen."""
    ids = store.expired_trash(time.time() - trash_days(store) * 86400)
    for job_id in ids:
        for name in store.purge_job(job_id):
            (image_dir / name).unlink(missing_ok=True)
    return len(ids)


def _html(text: str) -> web.Response:
    return web.Response(text=text, content_type="text/html")


def _job_id(request: web.Request) -> int:
    try:
        return int(request.match_info.get("job_id") or request.query.get("id") or "")
    except ValueError:
        raise web.HTTPNotFound() from None


async def _live_states(ha: Any, store: Store) -> dict[str, Any] | None:
    """Aktuelle Zustände der eingerichteten Entitäten, None ohne HA-Verbindung."""
    from . import printers as pcfg

    if not getattr(ha, "connected", False):
        return None
    ids = sorted({x for c in pcfg.load(store) for x in pcfg.entities(c)})

    async def one(entity: str) -> tuple[str, Any]:
        try:
            st = await ha.get_state(entity)
        except Exception:  # noqa: BLE001
            return entity, None
        return entity, (st or {}).get("state") if st else None

    try:
        return dict(await asyncio.wait_for(asyncio.gather(*(one(i) for i in ids)), 5))
    except asyncio.TimeoutError:
        return None


def _save_printers(store: Store, form: Any) -> str | None:
    """Einstellungen → Drucker speichern. Gibt einen Hinweis zurück oder None."""
    from . import printers as pcfg

    if not any(k.startswith(("pr__", "prnew__")) for k in form):
        return None
    fields = ("name", "status", "energy", "fixed_w", "ams", "ext", "prefix")
    out, msgs = [], []
    for c in pcfg.load(store):
        f = f"pr__{c['key']}"
        if f"{f}__name" not in form:
            out.append(c)  # nicht im Formular (z. B. gerade aus einem Event gelernt)
            continue
        if form.get(f"{f}__remove"):
            msgs.append(t("Drucker {key} entfernt (seine Drucke bleiben).", key=c["key"]))
            continue
        item = {"key": c["key"], **{k: str(form.get(f"{f}__{k}") or "").strip() for k in fields}}
        item["fixed_w"] = parse_num(item["fixed_w"])
        out.append(pcfg.normalize(item) or c)
    key = str(form.get("prnew__key") or "").strip().lower()
    if key:
        if not pcfg.KEY_RE.fullmatch(key):
            msgs.append(t("Kurzname „{key}“ geht nicht: nur a–z, 0–9 und _, höchstens 20 Zeichen.", key=key))
        elif any(c["key"] == key for c in out):
            msgs.append(t("Den Drucker „{key}“ gibt es schon.", key=key))
        else:
            item = {"key": key, **{k: str(form.get(f"prnew__{k}") or "").strip() for k in fields}}
            item["fixed_w"] = parse_num(item["fixed_w"])
            out.append(pcfg.normalize(item))
            msgs.append(t("Drucker {key} angelegt.", key=key))
    pcfg.save(store, out)
    return " ".join(msgs) or None


def _delta(v: Any) -> float | None:
    """Schritt der −/+-Knöpfe mit Vorzeichen (parse_num lässt nur Werte ≥ 0 zu)."""
    v = str(v or "").strip()
    neg = v.startswith(("-", "−"))
    n = parse_num(v[1:] if neg else v)
    return None if n is None else (-n if neg else n)


def setup_routes(app: web.Application, store: Store, image_dir: Path, ha_state: Any, demo: bool = False) -> None:
    """Alle Routen. Mit ``demo`` (Demo-Modus unter /demo/) bleibt das Backup ins Netzwerkziel aus."""
    routes = web.RouteTableDef()

    @web.middleware
    async def language(request: web.Request, handler: Any) -> web.StreamResponse:
        """Sprache pro Anfrage: Einstellung (de/en) oder bei „auto“ die Sprache des Browsers."""
        cfg = store.settings()
        token = LANG.set(resolve(cfg.get("language"), request.headers.get("Accept-Language")))
        theme = THEME.set(cfg.get("theme") if cfg.get("theme") in THEMES else "auto")
        hints = HINTS.set(mdl.hint_count(store) if request.method == "GET" else None)
        try:
            return await handler(request)
        finally:
            if request.method == "POST" and not request.path.endswith(("/photos", "/logo", "/restore")):
                # Kunde, Geschenk, Projekt … geändert: Designkosten der Modelle neu verteilen
                try:
                    mdl.apply_all(store)
                except Exception:  # noqa: BLE001
                    log.exception("Designkosten neu verteilen fehlgeschlagen")
            HINTS.reset(hints)
            THEME.reset(theme)
            LANG.reset(token)

    app.middlewares.append(language)

    async def in_thread(func: Any, *args: Any) -> Any:
        """Im Thread ausführen, mit der Sprache dieser Anfrage."""
        ctx = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(None, ctx.run, func, *args)

    # ---- Seiten ---------------------------------------------------------------

    @routes.get("/")
    async def index(request: web.Request) -> web.Response:
        f = _filters(request)
        if request.query.get("partial"):
            return _html(results(store, f))
        return _html(page_jobs(store, f, ha_state.connected))

    @routes.get("/job")
    async def job_page(request: web.Request) -> web.Response:
        job = store.get(_job_id(request))
        if not job:
            raise web.HTTPNotFound(text=t("Druck nicht gefunden"))
        return _html(page_job(store, job, request.query.get("saved")))

    @routes.get("/go")
    async def go(request: web.Request) -> web.Response:
        """Projekt-ID (z. B. PL-7K3Q, auch ohne Präfix, Groß/klein egal) → Detailseite des Drucks."""
        q = request.query.get("q") or ""
        item = lg.parse_item_code(q)
        if item and not request.query.get("json"):
            raise web.HTTPSeeOther(f"{'loc' if item[0] == 'L' else 'item'}?c={item[1]}")
        job_id = store.find_code(q)
        if request.query.get("json"):
            return web.json_response({"id": job_id})
        if job_id:
            raise web.HTTPSeeOther(f"job?id={job_id}")
        raise web.HTTPSeeOther("./?" + urlencode({"q": q}) if q else "./")

    @routes.get("/export/{job_id:\\d+}/{kind}")
    async def export(request: web.Request) -> web.Response:
        """Bon-Bild (für Kunden / intern) oder PDF eines Drucks; ?dl=1 als Download, ?status=0 ohne Zahlung/Übergabe."""
        job = store.get(_job_id(request))
        if not job:
            raise web.HTTPNotFound(text=t("Druck nicht gefunden"))
        kind = request.match_info["kind"]
        data = collect(store, job, image_dir, status=request.query.get("status") != "0",
                       positions=request.query.get("pos") == "1")
        if kind in ("bon-kunde.png", "bon-intern.png"):
            variant = kind[4:-4]
            body = await in_thread(bon_png, data, variant)
            ctype, name = "image/png", file_name(job, variant, "png")
        elif kind == "kosten.pdf":
            body = await in_thread(report_pdf, data)
            ctype, name = "application/pdf", file_name(job, "kosten", "pdf")
        else:
            raise web.HTTPNotFound()
        disp = "attachment" if request.query.get("dl") else "inline"
        return web.Response(body=body, content_type=ctype, headers={
            "Content-Disposition": f'{disp}; filename="{name}"', "Cache-Control": "no-store"})

    @routes.get("/customers")
    async def customers(request: web.Request) -> web.Response:
        q = request.query
        return _html(page_customers(store, q.get("saved"), q.get("show"), q.get("period"), q.get("von"), q.get("bis")))

    @routes.get("/customer")
    async def customer(request: web.Request) -> web.Response:
        cid = request.query.get("id", "")
        html = page_customer(store, int(cid), request.query.get("period")) if cid.isdigit() else None
        if html is None:
            raise web.HTTPSeeOther("customers")
        return _html(html)

    @routes.post("/customers")
    async def save_customer(request: web.Request) -> web.Response:
        form = await request.post()
        name = str(form.get("name") or "").strip()
        if not name:
            raise web.HTTPSeeOther("customers")
        cid = str(form.get("id") or "")
        store.save_customer(
            int(cid) if cid.isdigit() else None,
            name,
            str(form.get("contact") or "").strip() or None,
            str(form.get("note") or "").strip() or None,
            parse_num(form.get("margin_pct")),
            str(form.get("no_billing") or "") == "1",
            {"1": 1, "0": 0}.get(str(form.get("work_margin") or "")),
        )
        raise web.HTTPSeeOther("customers?saved=1")

    @routes.post("/customers/{cid:\\d+}/delete")
    async def delete_customer(request: web.Request) -> web.Response:
        store.delete_customer(int(request.match_info["cid"]))
        raise web.HTTPSeeOther("../../customers?saved=1")

    @routes.get("/settings")
    async def settings(request: web.Request) -> web.Response:
        live = await _live_states(ha_state, store)
        q = request.query
        data_msg = {k: q[k] for k in ("restored", "reset") if archive.safety_path(data_dir, q.get(k, ""))}
        return _html(page_settings(store, q.get("saved"), q.get("logo"), live, q.get("pmsg"),
                                   archive.safety_files(data_dir), data_msg))

    @routes.post("/settings")
    async def save_settings(request: web.Request) -> web.Response:
        form = await request.post()
        pmsg = _save_printers(store, form)
        for printer in printer_names(store):
            store.set_printer_rate(
                printer, **{fld: parse_num(form.get(f"{fld}__{printer}")) for fld in Store.RATE_FIELDS}
            )
        for key, value in form.items():
            if key.startswith("type__"):
                store.set_material_price(key[6:], "type", parse_num(value))
            elif key.startswith("name__"):
                store.set_material_price(key[6:], "name", parse_num(value))
        if "margin1_name" in form:
            store.set_setting("work_margin_default", "1" if form.get("work_margin_default") else "0")
        wdflt = "0" if store.settings().get("work_margin_default") == "0" else "1"
        for i in (1, 2):
            store.set_setting(f"margin{i}_name", str(form.get(f"margin{i}_name") or "").strip() or None)
            pct = parse_num(form.get(f"margin{i}_pct"))
            store.set_setting(f"margin{i}_pct", None if pct is None else f"{pct:g}")
            if f"margin{i}_name" in form:
                store.set_setting(f"margin{i}_on", None if form.get(f"margin{i}_on") else "0")  # ohne Wert = aktiv
                w = "1" if form.get(f"margin{i}_work") else "0"
                store.set_setting(f"margin{i}_work", None if w == wdflt else w)  # wie Standard: folgt ihm
        zk.save_settings(store, form)
        days = str(form.get("trash_days") or "").strip()
        if days.isdigit() and 1 <= int(days) <= 3650:
            store.set_setting("trash_days", days)
        if "stock_price" in form:
            store.set_setting("stock_price", str(form["stock_price"]) if form.get("stock_price") in ("cost", "first", "ask") else "cost")
        if form.get("detail_tab") in DETAIL_TABS:
            store.set_setting("detail_tab", str(form["detail_tab"]))
        if "language" in form:
            choice = str(form.get("language") or "")
            store.set_setting("language", choice if choice in LANGS else "auto")
        if "hint_notify" in form:
            hn = str(form.get("hint_notify") or "").strip()
            store.set_setting("hint_notify", hn if re.fullmatch(r"[a-z_]+(\.[a-z0-9_]+)?", hn) else None)
        if form.get("order_auto") in od.AUTO_MODES:
            store.set_setting("order_auto", str(form["order_auto"]))
        if "theme" in form:
            store.set_setting("theme", str(form["theme"]) if form.get("theme") in THEMES else "auto")
        for key in ("backup_interval", "backup_weekday", "backup_hour"):
            if key in form:
                store.set_setting(key, str(form.get(key) or "").strip() or None)
        logo_msg = ""
        old_logo = store.settings().get("bon_logo")
        upload = form.get("bon_logo")
        if form.get("bon_logo_reset"):
            store.set_setting("bon_logo", None)
            logo_msg = "&logo=reset"
        elif upload is not None and getattr(upload, "filename", None):
            raw = upload.file.read(MAX_PHOTO_BYTES + 1)
            name = None
            if 0 < len(raw) <= MAX_PHOTO_BYTES:
                name = await asyncio.get_running_loop().run_in_executor(
                    None, save_logo, raw, image_dir, bool(form.get("bon_logo_white")))
            if name:
                store.set_setting("bon_logo", name)
                logo_msg = "&logo=ok"
            else:
                logo_msg = "&logo=bad"
        new_logo = store.settings().get("bon_logo")
        if old_logo and old_logo != new_logo:
            (image_dir / old_logo).unlink(missing_ok=True)
        n = recompute_all(store)
        tab = str(form.get("tab") or "")
        anchor = f"#{tab}" if re.fullmatch(r"[a-z]{1,12}", tab) else ""
        pmsg_q = f"&pmsg={quote(pmsg)}" if pmsg else ""
        raise web.HTTPSeeOther(f"settings?saved={n}{logo_msg}{pmsg_q}{anchor}")

    @routes.post("/backup")
    async def backup(request: web.Request) -> web.Response:
        from .backup import backup_now, enabled

        if enabled() and not demo:
            await backup_now(store, image_dir)
        raise web.HTTPSeeOther("settings")

    # ---- Daten & Backup: ZIP herunterladen, einspielen, zurücksetzen ------------

    data_dir = image_dir.parent
    reset_codes: dict[str, tuple[str, float]] = {}
    busy = asyncio.Lock()  # nie zwei Einspiel-/Zurücksetz-Vorgänge gleichzeitig

    def _err(exc: Exception) -> str:
        if isinstance(exc, archive.ArchiveError):
            return t(exc.key, **exc.values)
        return t("Unerwarteter Fehler: {msg}", msg=str(exc)[:200])

    async def _send_file(request: web.Request, path: Path, name: str, delete: bool = False) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(path.stat().st_size),
            "Cache-Control": "no-store",
        })
        await resp.prepare(request)
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    await resp.write(chunk)
            await resp.write_eof()
        finally:
            if delete:
                path.unlink(missing_ok=True)
        return resp

    @routes.get("/backup.zip")
    async def backup_zip(request: web.Request) -> web.StreamResponse:
        archive.cleanup_stage(data_dir)
        name = f"printlog-{time.strftime('%Y-%m-%d_%H%M')}.zip"
        tmp = archive.stage_dir(data_dir) / f"export-{archive.new_token()}.zip"
        await in_thread(archive.build_zip, store, image_dir, tmp)
        return await _send_file(request, tmp, name, delete=True)

    @routes.get("/safety/{name}")
    async def safety_download(request: web.Request) -> web.StreamResponse:
        path = archive.safety_path(data_dir, request.match_info["name"])
        if path is None:
            raise web.HTTPNotFound()
        return await _send_file(request, path, path.name)

    def _current() -> dict[str, Any]:
        cur = store.counts()
        cur["images"] = sum(1 for p in image_dir.iterdir() if p.is_file()) if image_dir.is_dir() else 0
        return cur

    async def _preview(zip_path: Path, source: str | None) -> web.Response:
        token = archive.new_token()
        try:
            info = await in_thread(archive.unpack, zip_path, archive.stage_dir(data_dir) / token)
        except Exception as exc:  # noqa: BLE001
            import shutil
            shutil.rmtree(archive.stage_dir(data_dir) / token, ignore_errors=True)
            return _html(page_import(_current(), None, None, _err(exc)))
        return _html(page_import(_current(), info, token, None, source))

    @routes.post("/import")
    async def import_upload(request: web.Request) -> web.Response:
        """ZIP hochladen (gestreamt, ohne Größenlimit von aiohttp), prüfen, Vorschau zeigen."""
        archive.cleanup_stage(data_dir)
        tmp = archive.stage_dir(data_dir) / f"upload-{archive.new_token()}.zip"
        try:
            reader = await request.multipart()
            field = await reader.next()
            while field is not None and getattr(field, "name", None) != "file":
                field = await reader.next()
            if field is None or not getattr(field, "filename", None):
                return _html(page_import(_current(), None, None, t("Bitte eine ZIP-Datei auswählen.")))
            size = 0
            with open(tmp, "wb") as fh:
                while chunk := await field.read_chunk(1024 * 1024):  # type: ignore[union-attr]
                    size += len(chunk)
                    if size > archive.MAX_UPLOAD:
                        return _html(page_import(_current(), None, None, t("Die Datei ist zu groß.")))
                    fh.write(chunk)
            return await _preview(tmp, None)
        finally:
            tmp.unlink(missing_ok=True)

    @routes.post("/import-safety")
    async def import_safety(request: web.Request) -> web.Response:
        form = await request.post()
        path = archive.safety_path(data_dir, str(form.get("name") or ""))
        if path is None:
            raise web.HTTPNotFound()
        return await _preview(path, path.name)

    @routes.post("/import/apply")
    async def import_apply(request: web.Request) -> web.Response:
        form = await request.post()
        if not form.get("confirm"):
            return _html(page_import(_current(), None, None, t("Bitte das Einspielen bestätigen.")))
        async with busy:
            try:
                res = await in_thread(archive.apply, store, image_dir, data_dir, str(form.get("token") or ""))
            except Exception as exc:  # noqa: BLE001
                log.exception("Einspielen fehlgeschlagen")
                return _html(page_import(_current(), None, None, _err(exc)))
        raise web.HTTPSeeOther(f"../settings?restored={quote(res['safety'])}#backup")

    def _new_reset_code() -> tuple[str, str]:
        import secrets

        from .db import CODE_ALPHABET
        now = time.time()
        for k in [k for k, (_, ts) in reset_codes.items() if now - ts > 1800]:
            reset_codes.pop(k, None)
        token = archive.new_token()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        reset_codes[token] = (code, now)
        return token, code

    @routes.get("/reset")
    async def reset_page(request: web.Request) -> web.Response:
        token, code = _new_reset_code()
        return _html(page_reset(store.counts(), token, code))

    @routes.post("/reset")
    async def reset_do(request: web.Request) -> web.Response:
        form = await request.post()
        entry = reset_codes.pop(str(form.get("token") or ""), None)
        scope = form.get("scope")
        typed = re.sub(r"\s", "", str(form.get("code") or "")).upper()
        error = None
        if entry is None or time.time() - entry[1] > 1800:
            error = t("Der Code ist abgelaufen. Bitte den neuen Code eintippen.")
        elif scope not in ("history", "all"):
            error = t("Bitte auswählen, was gelöscht werden soll.")
        elif typed != entry[0]:
            error = t("Der Code stimmt nicht. Es wurde nichts gelöscht.")
        if error:
            token, code = _new_reset_code()
            return _html(page_reset(store.counts(), token, code, error))
        async with busy:
            safety = await in_thread(archive.reset, store, image_dir, data_dir, scope == "all")
        raise web.HTTPSeeOther(f"settings?reset={quote(safety)}#backup")

    @routes.get("/stats")
    async def stats(request: web.Request) -> web.Response:
        return _html(page_stats(store, request.query.get("period", "month")))

    @routes.get("/calendar")
    async def calendar(request: web.Request) -> web.Response:
        return _html(page_calendar(store, request.query.get("month")))

    @routes.get("/export.csv")
    async def export_csv(request: web.Request) -> web.Response:
        # Deutsch: Semikolon und Dezimalkomma (Excel mit deutscher Einstellung), Englisch: Komma und Punkt.
        en = is_en()
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter="," if en else ";")
        for row in csv_rows(store, _filters(request)):
            writer.writerow(
                [(str(v) if en else str(v).replace(".", ",")) if isinstance(v, float) else ("" if v is None else v)
                 for v in row]
            )
        return web.Response(
            body=("﻿" + buf.getvalue()).encode("utf-8"),
            content_type="text/csv",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{t("druckkosten")}_{time.strftime("%Y-%m-%d")}.csv"'
            },
        )

    # ---- API ------------------------------------------------------------------

    @routes.get("/api/jobs")
    async def api_jobs(request: web.Request) -> web.Response:
        f = _db_filters(_filters(request))
        limit = min(int(request.query.get("limit", 500)), 5000)
        offset = int(request.query.get("offset", 0))
        jobs = store.list_jobs(limit=limit, offset=offset, **f)
        photos = store.photo_counts([j["id"] for j in jobs])
        for j in jobs:
            j["materials"] = parse_materials(j.get("materials"))
            j["photos"] = photos.get(j["id"], 0)
        return web.json_response(jobs)

    @routes.get("/api/summary")
    async def api_summary(request: web.Request) -> web.Response:
        return web.json_response(store.summary(**_db_filters(_filters(request))))

    @routes.post("/api/jobs/{job_id:\\d+}")
    async def api_update(request: web.Request) -> web.Response:
        job_id = _job_id(request)
        job = store.get(job_id)
        if not job:
            raise web.HTTPNotFound()
        form = await request.post()
        fields: dict[str, Any] = {}
        if "task_name" in form:
            fields["task_name"] = str(form["task_name"]).strip() or job["task_name"]
        if "customer_id" in form:
            cid = str(form["customer_id"])
            fields["customer_id"] = int(cid) if cid.isdigit() else None
        if "quantity" in form:
            q = str(form["quantity"]).strip()
            fields["quantity"] = int(q) if q.isdigit() and int(q) > 0 else None
        if "job_note" in form:
            fields["job_note"] = str(form["job_note"]).strip() or None
        if "scrap" in form:
            sv = str(form["scrap"])
            fields["scrap"] = int(sv) if sv in ("0", "1") else None
        if "project_url" in form:
            url = str(form["project_url"]).strip() or None
            title = str(form.get("project_title") or "").strip() or None
            if url and not title:
                title = await fetch_title(app.get("session"), url) or title_from_url(url)
            fields["project_url"] = url
            fields["project_title"] = title
        elif "project_title" in form:
            fields["project_title"] = str(form["project_title"]).strip() or None
        if "local_path" in form:
            fields["local_path"] = clean_local_path(str(form["local_path"]))
        if "final_price" in form:
            fields["final_price"] = parse_num(form["final_price"])
        if any(str(k).startswith("mnote__") for k in form):
            notes = {k[7:]: str(v).strip()[:120] for k, v in form.items()
                     if k.startswith("mnote__") and k[7:].isdigit() and str(v).strip()}
            fields["material_notes"] = json.dumps(notes, ensure_ascii=False) if notes else None
        if "gift" in form:
            fields["gift"] = 1 if str(form["gift"]) == "1" else None
        if "defect" in form:
            # Fehldruck-Ursache; eine Ursache zu wählen macht den Druck zum Fehldruck
            dv = str(form["defect"])
            fields["defect"] = dv if dv in DEFECTS else None
            if fields["defect"] and not job.get("is_scrap"):
                fields["scrap"] = 1
        if "defect_charge" in form:
            mode = str(form["defect_charge"])
            if mode == "full":
                fields["defect_charge"] = "full"
            elif mode == "part":
                unit = str(form.get("defect_unit") or ("eur" if job.get("defect_charge") == "amount" else "pct"))
                val = parse_num(form.get("defect_value")) if "defect_value" in form else job.get("defect_value")
                if job.get("defect_charge") not in ("pct", "amount") and "defect_value" not in form:
                    unit, val = "pct", 50.0  # erster Klick auf „Anteilig“: halber Preis
                if unit == "eur":
                    fields["defect_charge"], fields["defect_value"] = "amount", max(0.0, round(val or 0.0, 2))
                else:
                    fields["defect_charge"], fields["defect_value"] = "pct", min(100.0, max(0.0, val if val is not None else 50.0))
            else:
                fields["defect_charge"] = None
        if "reprint" in form:
            rv = str(form["reprint"])
            store.set_reprint(job_id, int(rv) if rv.isdigit() else None)
        if fields:
            store.update(job_id, **fields)
            if "quantity" in fields:
                # Zubehör pro Stück und Nachbearbeitung hängen an der Stückzahl
                zk.sync_stock(store, job_id)
                zk.refresh(store, job_id)
        if request.query.get("redirect"):
            tab = "#ab" if request.query.get("redirect") == "ab" else ""
            raise web.HTTPSeeOther(f"../../job?id={job_id}&saved=1{tab}")
        job = store.get(job_id) or job
        return web.json_response({"ok": True, "per_piece": _per_piece(job)})

    @routes.post("/api/jobs/{job_id:\\d+}/handover")
    async def api_handover(request: web.Request) -> web.Response:
        """Übergabe eintragen: qty = Zahl oder "rest"; reset=1 löscht alle Übergaben."""
        job_id = _job_id(request)
        job = store.get(job_id)
        if not job:
            raise web.HTTPNotFound()
        form = await request.post()
        if str(form.get("reset") or "") == "1":
            store.clear_handovers(job_id)
        else:
            rest = max(0, (job.get("quantity") or 1) - (job.get("delivered") or 0))
            raw = str(form.get("qty") or "rest").strip()
            qty = rest if raw == "rest" else (int(raw) if raw.isdigit() else 0)
            ts = _day_ts(str(form.get("date") or ""))
            if qty > 0:
                store.add_handover(job_id, qty, ts, str(form.get("note") or "").strip() or None)
                zk.on_delivered(store, job_id)
        if request.query.get("redirect"):
            raise web.HTTPSeeOther(f"../../../job?id={job_id}#ab")
        job = store.get(job_id) or job
        return web.json_response({"ok": True, "delivered": job.get("delivered"), "quantity": job.get("quantity") or 1})

    @routes.post("/api/jobs/{job_id:\\d+}/pay")
    async def api_pay(request: web.Request) -> web.Response:
        """Zahlung eintragen: rest=1 bucht den offenen Rest, amount=Betrag; reset=1 löscht alle Zahlungen."""
        job_id = _job_id(request)
        job = store.get(job_id)
        if not job:
            raise web.HTTPNotFound()
        form = await request.post()
        if str(form.get("reset") or "") == "1":
            store.clear_payments(job_id)
        else:
            due = amount_due(job)
            amount = (max(0.0, (due or 0) - (job.get("paid") or 0)) if str(form.get("rest") or "") == "1"
                      else parse_num(form.get("amount")))
            if amount and amount > 0:
                store.add_payment(job_id, amount, _day_ts(str(form.get("date") or "")),
                                  str(form.get("note") or "").strip() or None)
        if request.query.get("redirect"):
            raise web.HTTPSeeOther(f"../../../job?id={job_id}#ab")
        return web.json_response({"ok": True})

    @routes.post("/api/jobs/{job_id:\\d+}/work")
    async def api_work(request: web.Request) -> web.Response:
        """Arbeitszeit: field = prep_min | post_min | design_min | post_once, value oder delta (Minuten)."""
        job_id = _job_id(request)
        job = store.get(job_id)
        if not job:
            raise web.HTTPNotFound()
        form = await request.post()
        fld = str(form.get("field") or "")
        if fld == "post_once":
            store.update(job_id, post_once=1 if str(form.get("value")) == "1" else None)
        elif fld == "design_min" and job.get("model_id") and mdl.model(store, job["model_id"]):
            # Designzeit gehört zum Modell, nicht zum einzelnen Druck
            m = mdl.model(store, job["model_id"]) or {}
            delta = _delta(form.get("delta"))
            val = (m.get("design_min") or 0) + delta if delta is not None else parse_num(form.get("value"))
            mdl.save_model(store, m["id"], design_min=None if not val or val <= 0 else round(val, 2))
        elif fld == "design_mode" and job.get("model_id"):
            mdl.save_model(store, job["model_id"], design_mode="spread" if str(form.get("value")) == "spread" else None)
        elif fld in ("prep_min", "post_min", "design_min"):
            delta = _delta(form.get("delta"))
            val = (job.get(fld) or 0) + delta if delta is not None else parse_num(form.get("value"))
            store.update(job_id, **{fld: None if not val or val <= 0 else round(val, 2)})
        zk.refresh(store, job_id)
        raise web.HTTPSeeOther(f"../../../job?id={job_id}#zk")

    @routes.post("/api/jobs/{job_id:\\d+}/model")
    async def api_model(request: web.Request) -> web.Response:
        """Modell-Hinweis beantworten: model = ID (bestätigen/zuordnen) | do = new (eigenes Modell) | keep."""
        job_id = _job_id(request)
        if not store.get(job_id):
            raise web.HTTPNotFound()
        form = await request.post()
        mid = str(form.get("model") or "")
        do = str(form.get("do") or "")
        if mid.isdigit():
            mdl.assign(store, job_id, int(mid))
        elif do == "new":
            mdl.make_new(store, job_id)
        elif do == "keep":
            mdl.dismiss(store, job_id)
        back = str(form.get("back") or "")
        if not re.fullmatch(r"(hints|models|job\?id=\d+)", back):
            back = f"job?id={job_id}"
        raise web.HTTPSeeOther("../../../" + back)

    @routes.post("/api/models/{model_id:\\d+}")
    async def api_model_edit(request: web.Request) -> web.Response:
        """Modell bearbeiten: name, design_min, design_mode, spread_n | merge = Ziel-ID | unalias = Alias."""
        mid = int(request.match_info["model_id"])
        if not mdl.model(store, mid):
            raise web.HTTPNotFound()
        form = await request.post()
        target = mid
        if str(form.get("merge") or "").isdigit():
            target = int(str(form["merge"]))
            mdl.merge(store, mid, target)
        elif "unalias" in form:
            mdl.remove_alias(store, mid, str(form["unalias"]))
        else:
            f: dict[str, Any] = {}
            name = str(form.get("name") or "").strip()
            if name:
                f["name"] = name[:120]
            if "design_min" in form:
                dm = parse_num(form.get("design_min"))
                f["design_min"] = None if not dm or dm <= 0 else round(dm, 2)
            if "design_mode" in form:
                f["design_mode"] = "spread" if str(form["design_mode"]) == "spread" else None
            if "spread_n" in form:
                n = parse_num(form.get("spread_n"))
                f["spread_n"] = max(2, min(int(n), 999)) if n else None
            if "fav" in form:
                f["fav"] = 1 if str(form["fav"]) == "1" else None
            mdl.save_model(store, mid, **f)
        back = str(form.get("back") or "")
        if re.fullmatch(r"models(\?[\w=&%-]*)?", back):
            raise web.HTTPSeeOther(f"../../{back}#m{target}")
        if re.fullmatch(r"customer\?id=\d+", back):
            raise web.HTTPSeeOther(f"../../{back}")
        if re.fullmatch(r"job\?id=\d+", back):
            raise web.HTTPSeeOther(f"../../{back}#zk")
        raise web.HTTPSeeOther(f"../../models#m{target}")

    @routes.get("/models")
    async def models_page(request: web.Request) -> web.Response:
        q = request.query
        cid = q.get("c")
        return _html(mdl.page_models(store, q.get("q"), q.get("f"), int(cid) if cid and cid.isdigit() else None))

    # ---- Aufträge -------------------------------------------------------------

    def _customer(form: Any) -> int | None:
        name = str(form.get("customer") or "").strip()
        return store.customer_by_name(name) if name else None

    def _item(form: Any) -> tuple[int | None, str]:
        name = str(form.get("item") or "").strip()
        return (od.model_by_name(store, name) if name else None), name

    @routes.get("/orders")
    async def orders_page(request: web.Request) -> web.Response:
        cid = request.query.get("c")
        return _html(od.page_orders(store, int(cid) if cid and cid.isdigit() else None, bool(request.query.get("archiv"))))

    @routes.get("/order")
    async def order_page(request: web.Request) -> web.Response:
        q = request.query
        if q.get("new"):
            cid = q.get("c")
            return _html(od.page_new(store, int(cid) if cid and cid.isdigit() else None))
        o = od.order(store, int(q["id"])) if str(q.get("id") or "").isdigit() else None
        if not o:
            raise web.HTTPNotFound(text=t("Auftrag nicht gefunden"))
        return _html(od.page_order(store, o, q.get("saved"), bool(q.get("link"))))

    @routes.post("/orders")
    async def order_create(request: web.Request) -> web.Response:
        form = await request.post()
        oid = od.create_order(store, _customer(form), str(form.get("title") or ""), form.get("due"),
                              parse_num(form.get("agreed")), str(form.get("note") or ""))
        mid, name = _item(form)
        if mid or name:
            od.add_item(store, oid, mid, name, int(parse_num(form.get("qty")) or 1))
        raise web.HTTPSeeOther(f"order?id={oid}")

    @routes.post("/orders/answer")
    async def order_answer(request: web.Request) -> web.Response:
        """Antwort auf „Für welchen Auftrag?“ (Glocke): item = Position, leer = kein Auftrag."""
        form = await request.post()
        job = str(form.get("job") or "")
        if not job.isdigit() or not store.get(int(job)):
            raise web.HTTPBadRequest()
        item = str(form.get("item") or "")
        if item and not (item.isdigit() and od.item_exists(store, int(item))):
            raise web.HTTPBadRequest()
        od.answer(store, int(job), int(item) if item else None)
        back = str(form.get("back") or "")
        if not re.fullmatch(r"(hints|job\?id=\d+)", back):
            back = "hints"
        raise web.HTTPSeeOther("../" + back)

    @routes.post("/orders/again")
    async def order_again(request: web.Request) -> web.Response:
        form = await request.post()
        mid = str(form.get("model") or "")
        if not mid.isdigit() or not mdl.model(store, int(mid)):
            raise web.HTTPSeeOther("../orders")
        into = str(form.get("into") or "")
        oid = od.again(store, int(mid), _customer(form), int(parse_num(form.get("qty")) or 1),
                       int(into) if into.isdigit() else None, form.get("due"))
        raise web.HTTPSeeOther(f"../order?id={oid}&saved=1")

    @routes.post("/orders/{oid:\\d+}")
    async def order_edit(request: web.Request) -> web.Response:
        oid = int(request.match_info["oid"])
        o = od.order(store, oid)
        if not o:
            raise web.HTTPNotFound()
        form = await request.post()
        do = str(form.get("do") or "")
        item = int(str(form["item"])) if str(form.get("item") or "").isdigit() else None
        if item is not None and not any(i["id"] == item for i in o["items"]):
            raise web.HTTPBadRequest()
        job = int(str(form["job"])) if str(form.get("job") or "").isdigit() else None
        saved, extra = "1", ""
        if do == "save":
            od.save_order(store, oid, customer_id=_customer(form), title=str(form.get("title") or ""), due=form.get("due"),
                          agreed_price=parse_num(form.get("agreed")), note=str(form.get("note") or "").strip() or None)
        elif do == "add":
            mid, name = _item(form)
            if mid or name:
                od.add_item(store, oid, mid, name, int(parse_num(form.get("qty")) or 1))
        elif do == "qty" and item:
            it = next(i for i in o["items"] if i["id"] == item)
            od.set_item(store, item, qty=it["qty"] + (1 if str(form.get("d")) == "1" else -1))
        elif do == "del_item" and item:
            od.delete_item(store, item)
        elif do == "link" and job and item:
            od.link(store, job, item)
            extra = "&link=1#od"
        elif do == "unlink" and job:
            if any(j["id"] == job for i in o["items"] for j in i["jobs"]):
                od.link(store, job, None)
            extra = "#od"
        elif do == "cell" and item:
            it = next(i for i in o["items"] if i["id"] == item)
            m = re.fullmatch(r"(\d+):(\d+)(?::(print|stock|skip))?", str(form.get("cell") or ""))
            if m:
                pl.set_cell(store, it, int(m.group(1)), int(m.group(2)), m.group(3))
            extra = f"#pb{item}"
        elif do == "by_plate" and item:
            od.set_item(store, item, by_plate=1 if str(form.get("on")) == "1" else None)
            extra = f"#pb{item}"
        elif do == "plates" and item:
            it = next(i for i in o["items"] if i["id"] == item)
            n = parse_num(form.get("n_plates")) if "n_plates" in form else None
            rows = []
            for p in form.getall("plate", []):
                if str(p).isdigit():
                    part = None
                    pname = str(form.get(f"ppart_{p}") or "").strip()
                    if pname:
                        part = od.model_by_name(store, pname) or mdl.create_model(store, pname[:120])
                    row = {"plate": int(p), "name": form.get(f"pname_{p}"), "grp": form.get(f"pgrp_{p}"), "part_model_id": part}
                    if f"pcol_{p}" in form:
                        row["colors"] = form.get(f"pcol_{p}")
                    rows.append(row)
            pl.save_plates(store, it, rows, int(n) if n else (0 if "n_plates" in form else None))
            if str(form.get("on")) == "1" or rows:
                od.set_item(store, item, by_plate=1)
            extra = f"#pb{item}"
        elif do == "use_stock" and item:
            it = next(i for i in o["items"] if i["id"] == item)
            if str(form.get("plate") or "").isdigit():
                n = parse_num(form.get("n"))
                pl.use_stock(store, it, int(str(form["plate"])), int(n) if n else None)
            extra = f"#pb{item}"
        elif do == "bill":
            target, err = od.bill(store, oid, stock=str(form.get("stock") or "") == "1")
            if err:
                saved = err
        elif do == "close":
            od.save_order(store, oid, closed=1)
        elif do == "reopen":
            od.reopen(store, oid)
        elif do == "delete":
            od.delete_order(store, oid)
            raise web.HTTPSeeOther("../orders")
        raise web.HTTPSeeOther(f"../order?id={oid}&saved={quote(saved)}{extra}")

    @routes.get("/hints")
    async def hints_page(request: web.Request) -> web.Response:
        return _html(mdl.page_hints(store))

    @routes.post("/api/jobs/{job_id:\\d+}/extras")
    async def api_extras(request: web.Request) -> web.Response:
        """Zubehör: add = JSON [[Inventar-ID, Menge pro Stück], …] | name + amount (einmaliger Posten)
        | extra + qty (Menge ändern, 0 = entfernen) | copy = Druck-ID (Arbeit und Zubehör übernehmen)."""
        job_id = _job_id(request)
        if not store.get(job_id):
            raise web.HTTPNotFound()
        form = await request.post()
        if str(form.get("add") or "").startswith("["):
            try:
                items = json.loads(str(form["add"]))
            except ValueError:
                items = []
            for it in items[:50]:
                if isinstance(it, list) and len(it) == 2 and str(it[0]).isdigit():
                    qty = parse_num(it[1]) or 0
                    if qty > 0:
                        zk.add_extra(store, job_id, int(it[0]), qty, True)
        elif str(form.get("name") or "").strip():
            amount = parse_num(form.get("amount"))
            if amount is not None and amount >= 0:
                zk.add_extra(store, job_id, None, 1, False, str(form["name"]), round(amount, 2))
        elif str(form.get("extra") or "").isdigit():
            qty = parse_num(form.get("qty")) or 0
            zk.set_extra_qty(store, int(str(form["extra"])), qty)
        elif str(form.get("copy") or "").isdigit():
            zk.copy_from(store, job_id, int(str(form["copy"])))
        raise web.HTTPSeeOther(f"../../../job?id={job_id}#zk")

    @routes.post("/api/jobs/{job_id:\\d+}/mounted")
    async def api_mounted(request: web.Request) -> web.Response:
        job_id = _job_id(request)
        if not store.get(job_id):
            raise web.HTTPNotFound()
        form = await request.post()
        raw = str(form.get("mounted") or "0")
        zk.set_mounted(store, job_id, int(raw) if raw.isdigit() else 0)
        raise web.HTTPSeeOther(f"../../../job?id={job_id}#zk")

    @routes.post("/api/payments/{pid:\\d+}/delete")
    async def api_payment_delete(request: web.Request) -> web.Response:
        job_id = store.delete_payment(int(request.match_info["pid"]))
        raise web.HTTPSeeOther(f"../../../job?id={job_id}#ab" if job_id else "../../../")

    def _id_list(raw: Any) -> list[int]:
        return [int(x) for x in str(raw or "").split(",") if x.strip().isdigit()]

    @routes.get("/api/bulkpay")
    async def api_bulkpay_data(request: web.Request) -> web.Response:
        """Dialog „Zahlung für mehrere Posten“: Kunde (customer=ID) oder Mehrfachauswahl (ids=1,2,3)."""
        q = request.query
        picked = _id_list(q.get("ids")) if "ids" in q else None
        cid = q.get("customer", "")
        return web.json_response(bp.dialog_data(store, int(cid) if cid.isdigit() else None, picked))

    @routes.post("/api/bulkpay")
    async def api_bulkpay(request: web.Request) -> web.Response:
        form = await request.post()
        cid = str(form.get("customer") or "")
        if not cid.isdigit():
            return web.json_response({"error": t("Kunde nicht gefunden.")})
        return web.json_response(bp.book(
            store, int(cid), _id_list(form.get("ids")), parse_num(form.get("amount")) or 0.0, str(form.get("mode") or ""),
            str(form.get("surplus") or ""), str(form.get("use_credit") or "") == "1", _day_ts(str(form.get("date") or "")),
            str(form.get("note") or "").strip() or None))

    @routes.post("/api/bulkpay/undo")
    async def api_bulkpay_undo(request: web.Request) -> web.Response:
        form = await request.post()
        bp.undo(store, _id_list(form.get("payments")), _id_list(form.get("credits")))
        return web.json_response({"ok": True})

    @routes.post("/api/credits/{cid:\\d+}/delete")
    async def api_credit_delete(request: web.Request) -> web.Response:
        customer_id = store.delete_credit(int(request.match_info["cid"]))
        raise web.HTTPSeeOther(f"../../../customer?id={customer_id}#guthaben" if customer_id else "../../../customers")

    @routes.post("/api/handovers/{hid:\\d+}/delete")
    async def api_handover_delete(request: web.Request) -> web.Response:
        job_id = store.delete_handover(int(request.match_info["hid"]))
        raise web.HTTPSeeOther(f"../../../job?id={job_id}#ab" if job_id else "../../../")

    @routes.post("/api/jobs/{job_id:\\d+}/recompute")
    async def api_recompute(request: web.Request) -> web.Response:
        job = store.get(_job_id(request))
        if not job:
            raise web.HTTPNotFound()
        store.update(job["id"], **compute(job, store))
        return web.json_response(store.get(job["id"]))

    @routes.post("/api/jobs/{job_id:\\d+}/photos")
    async def api_photos(request: web.Request) -> web.Response:
        job_id = _job_id(request)
        if not store.get(job_id):
            raise web.HTTPNotFound()
        reader = await request.multipart()
        n = 0
        while (part := await reader.next()) is not None:
            if getattr(part, "name", None) != "photo" or not getattr(part, "filename", None):
                continue
            ctype = (part.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = IMAGE_TYPES.get(ctype)
            if not ext:
                suffix = Path(part.filename).suffix.lower()
                ext = ".jpg" if suffix == ".jpeg" else (suffix if suffix in IMAGE_TYPES.values() else None)
            if not ext:
                continue
            name = f"photo_{job_id}_{int(time.time())}_{n}{ext}"
            target = image_dir / name
            size = 0
            with target.open("wb") as fh:
                while chunk := await part.read_chunk(256 * 1024):
                    size += len(chunk)
                    if size > MAX_PHOTO_BYTES:
                        break
                    fh.write(chunk)
            if size == 0 or size > MAX_PHOTO_BYTES:
                target.unlink(missing_ok=True)
                continue
            target = await asyncio.get_running_loop().run_in_executor(None, shrink_photo, target)
            name = target.name
            store.add_photo(job_id, name)
            n += 1
        raise web.HTTPSeeOther(f"../../../job?id={job_id}&saved=photo{n}")

    @routes.post("/api/photos/{pid:\\d+}/delete")
    async def api_photo_delete(request: web.Request) -> web.Response:
        photo = store.delete_photo(int(request.match_info["pid"]))
        if not photo:
            raise web.HTTPSeeOther("../../../")
        (image_dir / photo["file"]).unlink(missing_ok=True)
        raise web.HTTPSeeOther(f"../../../job?id={photo['job_id']}")

    @routes.post("/api/jobs/{job_id:\\d+}/delete")
    async def api_delete(request: web.Request) -> web.Response:
        """In den Papierkorb (wiederherstellbar)."""
        store.trash_job(_job_id(request))
        ref = request.headers.get("Referer") or ""
        raise web.HTTPSeeOther("../../../" if (not ref or "/job" in ref) else ref)

    def _ids(form: Any) -> list[int]:
        return [int(x) for x in re.split(r"[,\s]+", str(form.get("ids") or "")) if x.isdigit()]

    @routes.post("/api/jobs/delete")
    async def api_delete_many(request: web.Request) -> web.Response:
        ids = [i for i in _ids(await request.post()) if store.trash_job(i)]
        return web.json_response({"ok": True, "ids": ids})

    @routes.get("/storage")
    async def storage_page(request: web.Request) -> web.Response:
        return _html(lg.page_storage(store, request.query.get("tab"), request.query.get("saved")))

    @routes.post("/storage")
    async def storage_save(request: web.Request) -> web.Response:
        zk.save_inventory(store, await request.post())
        recompute_all(store)
        raise web.HTTPSeeOther("storage?saved=1")

    @routes.post("/storage/locations")
    async def storage_locations(request: web.Request) -> web.Response:
        """Lagerort anlegen (ohne id), ändern oder löschen (delete=1)."""
        form = await request.post()
        lid = str(form.get("id") or "")
        name = str(form.get("name") or "").strip()
        note = str(form.get("note") or "").strip()[:200] or None
        if lid.isdigit():
            loc = lg.location(store, int(lid))
            if not loc:
                raise web.HTTPSeeOther("../storage?tab=orte")
            if form.get("delete"):
                lg.delete_location(store, loc["id"])
                raise web.HTTPSeeOther("../storage?tab=orte&saved=1")
            if name:
                lg.save_location(store, loc["id"], name, note)
            raise web.HTTPSeeOther(f"../loc?c={loc['code']}&saved=1")
        if name:
            new = lg.location(store, lg.create_location(store, name, note))
            if new:
                raise web.HTTPSeeOther(f"../loc?c={new['code']}&saved=1")
        raise web.HTTPSeeOther("../storage?tab=orte")

    @routes.post("/storage/item")
    async def storage_item(request: web.Request) -> web.Response:
        form = await request.post()
        aid = str(form.get("id") or "")
        a = zk.accessory(store, int(aid)) if aid.isdigit() else None
        if not a:
            raise web.HTTPSeeOther("../storage")
        lg.item_booking(store, a["id"], parse_num(form.get("aq")), parse_num(form.get("ap")), parse_num(form.get("take")))
        raise web.HTTPSeeOther(f"../item?c={a['code']}&saved=1")

    @routes.get("/loc")
    async def loc_page(request: web.Request) -> web.Response:
        """Feste Adresse eines Lagerorts (L-XXXX), z. B. für einen QR-Code."""
        item = lg.parse_item_code("L" + (request.query.get("c") or "")[-4:])
        loc = lg.by_code(store, "locations", item[1]) if item else None
        if not loc:
            raise web.HTTPSeeOther("storage?tab=orte")
        return _html(lg.page_location(store, loc, request.query.get("saved")))

    @routes.get("/item")
    async def item_page(request: web.Request) -> web.Response:
        """Feste Adresse eines Zubehör-Artikels (A-XXXX)."""
        item = lg.parse_item_code("A" + (request.query.get("c") or "")[-4:])
        a = lg.by_code(store, "accessories", item[1]) if item else None
        if not a:
            raise web.HTTPSeeOther("storage")
        return _html(lg.page_item(store, a, request.query.get("saved")))

    @routes.post("/api/locations/assign")
    async def api_assign_location(request: web.Request) -> web.Response:
        """Lagerort für Drucke (ids) oder ein Zubehör (acc) setzen. new_name legt den Lagerort vorher an."""
        form = await request.post()
        new_name = str(form.get("new_name") or "").strip()
        raw = str(form.get("location_id") or "")
        lid = lg.location_by_name(store, new_name) if new_name else int(raw) if raw.isdigit() else None
        acc = str(form.get("acc") or "")
        if acc.isdigit():
            prev = lg.assign_accessories(store, [int(acc)], lid)
        else:
            prev = lg.assign_jobs(store, _ids(form), lid)
        if not prev:
            raise web.HTTPBadRequest()
        loc = lg.location(store, lid) if lid else None
        return web.json_response({"ok": True, "location_id": lid, "name": loc["name"] if loc else None,
                                  "prev": [[i, v] for i, v in prev.items()]})

    @routes.post("/api/customers/assign")
    async def api_assign_customer(request: web.Request) -> web.Response:
        """Kunde für einen oder mehrere Drucke setzen (Schnellauswahl). new_name legt den Kunden vorher an."""
        form = await request.post()
        ids = _ids(form)
        new_name = str(form.get("new_name") or "").strip()
        cid = str(form.get("customer_id") or "")
        customer_id = store.customer_by_name(new_name) if new_name else int(cid) if cid.isdigit() else None
        prev = store.assign_customer(ids, customer_id)
        if not prev:
            raise web.HTTPBadRequest()
        name = next((c["name"] for c in store.customers() if c["id"] == customer_id), None) if customer_id else None
        return web.json_response({"ok": True, "customer_id": customer_id, "name": name,
                                  "prev": [[i, c] for i, c in prev.items()]})

    @routes.post("/api/jobs/{job_id:\\d+}/split")
    async def api_split(request: web.Request) -> web.Response:
        """Stückzahl aufteilen: rows = JSON [{sid, customer_id, new_name, qty}]; off=1 hebt die Aufteilung auf."""
        job_id = _job_id(request)
        if not store.get(job_id):
            raise web.HTTPNotFound()
        form = await request.post()
        if str(form.get("off") or "") == "1":
            store.unsplit(job_id)
            return web.json_response({"ok": True})
        try:
            raw = json.loads(str(form.get("rows") or "[]"))
            rows = []
            for r in raw:
                if r.get("project_id") or r.get("new_project"):
                    rows.append({"sid": int(r["sid"]) if r.get("sid") else None, "qty": int(r.get("qty") or 0),
                                 "project_id": int(r["project_id"]) if r.get("project_id") else None,
                                 "new_project": str(r.get("new_project") or "").strip()[:120] or None})
                    continue
                name = str(r.get("new_name") or "").strip()
                cid = store.customer_by_name(name) if name else int(r["customer_id"]) if r.get("customer_id") else None
                if cid is None:
                    continue
                rows.append({"sid": int(r["sid"]) if r.get("sid") else None, "customer_id": cid, "qty": int(r.get("qty") or 0)})
        except (ValueError, TypeError, KeyError, AttributeError):
            raise web.HTTPBadRequest() from None
        try:
            force = [int(x) for x in json.loads(str(form.get("force") or "[]"))]
        except (ValueError, TypeError):
            force = []
        err = store.set_split(job_id, rows, force)
        return web.json_response({"ok": not err, "error": t(err) if err else None})

    @routes.post("/api/jobs/{job_id:\\d+}/give")
    async def api_give(request: web.Request) -> web.Response:
        """Aus dem Vorrat abgeben (customer_id oder new_name) oder ohne Kunde ausbuchen (note = Grund)."""
        job_id = _job_id(request)
        form = await request.post()
        name = str(form.get("new_name") or "").strip()
        cid = str(form.get("customer_id") or "")
        customer_id = store.customer_by_name(name) if name else int(cid) if cid.isdigit() else None
        q, part = str(form.get("qty") or ""), str(form.get("part") or "")
        res = store.give(job_id, customer_id, int(q) if q.isdigit() else 0, part=int(part) if part.isdigit() else None,
                         final_price=parse_num(form.get("price")) if customer_id else None,
                         handed=bool(form.get("handed")), note=None if customer_id else str(form.get("note") or "").strip() or None)
        if isinstance(res, str):
            return web.json_response({"ok": False, "error": t(res)})
        return web.json_response({"ok": True, "id": res})

    @routes.post("/api/shares/{sid:\\d+}/delete")
    async def api_share_delete(request: web.Request) -> web.Response:
        """Ausbuchung oder leeren Anteil zurücknehmen (Stück gehen zurück in den Vorrat)."""
        sid = int(request.match_info["sid"])
        share = store.get(sid)
        if not share or share.get("kind") != "share":
            raise web.HTTPNotFound()
        store.remove_share(sid)
        raise web.HTTPSeeOther(f"../../../job?id={share['share_of']}#split")

    @routes.post("/api/projects")
    async def api_project_create(request: web.Request) -> web.Response:
        """Ausgewählte Drucke zu einem Projekt zusammenfassen (mode=new) oder einem Projekt hinzufügen (mode=add)."""
        form = await request.post()
        ids = _ids(form)
        into = None
        if str(form.get("mode") or "") == "add":
            iv = str(form.get("into") or "")
            if not iv.isdigit():
                raise web.HTTPBadRequest()
            into = int(iv)
        cid = str(form.get("customer_id") or "")
        q = str(form.get("quantity") or "").strip()
        pid = store.create_project(ids, name=str(form.get("name") or "").strip() or None,
                                   customer_id=int(cid) if cid.isdigit() else None,
                                   quantity=int(q) if q.isdigit() and int(q) > 0 else None, into=into)
        if pid is None:
            raise web.HTTPBadRequest()
        return web.json_response({"ok": True, "id": pid})

    @routes.post("/api/projects/{job_id:\\d+}/add")
    async def api_project_add(request: web.Request) -> web.Response:
        pid = _job_id(request)
        store.create_project(_ids(await request.post()), into=pid)
        raise web.HTTPSeeOther(f"../../../job?id={pid}&saved=1")

    @routes.post("/api/projects/{job_id:\\d+}/dissolve")
    async def api_project_dissolve(request: web.Request) -> web.Response:
        store.dissolve_project(_job_id(request))
        raise web.HTTPSeeOther("../../../")

    @routes.post("/api/jobs/{job_id:\\d+}/unproject")
    async def api_unproject(request: web.Request) -> web.Response:
        job_id = _job_id(request)
        pid = store.remove_from_project(job_id)
        back = request.query.get("back")
        raise web.HTTPSeeOther(f"../../../job?id={pid if back == 'project' and pid and store.get(pid) else job_id}&saved=1")

    @routes.get("/trash")
    async def trash_page(request: web.Request) -> web.Response:
        purge_expired(store, image_dir)
        return _html(page_trash(store, trash_days(store), request.query.get("done")))

    @routes.post("/trash/restore")
    async def trash_restore(request: web.Request) -> web.Response:
        form = await request.post()
        n = sum(store.restore_job(i) for i in _ids(form))
        if form.get("json"):
            return web.json_response({"ok": True, "n": n})
        raise web.HTTPSeeOther(f"../trash?done=r{n}")

    @routes.post("/trash/purge")
    async def trash_purge(request: web.Request) -> web.Response:
        form = await request.post()
        ids = [j["id"] for j in store.trashed()] if form.get("all") else _ids(form)
        for job_id in ids:
            for name in store.purge_job(job_id):
                (image_dir / name).unlink(missing_ok=True)
        raise web.HTTPSeeOther(f"../trash?done=p{len(ids)}")

    @routes.get("/tools/{name}")
    async def tool_download(request: web.Request) -> web.Response:
        # Hilfsdateien für Ordner-Links unter Windows (Einstellungen).
        name = request.match_info["name"]
        if name not in TOOLS:
            raise web.HTTPNotFound()
        if name.endswith(".cmd"):
            text = SETUP_CMD + (TOOLS_DIR / "printlog-open.ps1").read_text(encoding="utf-8")
        else:
            text = (TOOLS_DIR / name).read_text(encoding="utf-8")
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        # PowerShell 5 liest Skripte ohne BOM als ANSI, regedit erwartet UTF-16,
        # cmd verträgt keine BOM (das Skript darin schreibt die .ps1 selbst mit BOM).
        if name.endswith(".reg"):
            body = text.encode("utf-16")
        elif name.endswith(".cmd"):
            body = text.encode("utf-8")
        else:
            body = text.encode("utf-8-sig")
        return web.Response(body=body, content_type="application/octet-stream",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @routes.get("/manifest.webmanifest")
    async def manifest(_req: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            STATIC_DIR / "manifest.webmanifest",
            headers={"Content-Type": "application/manifest+json"},
        )

    @routes.get("/favicon.ico")
    async def favicon(_req: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "favicon-32.png", headers={"Content-Type": "image/png"})

    @routes.get("/health")
    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {"ha_connected": ha_state.connected, "last_event": ha_state.last_event},
            status=200 if ha_state.connected else 503,
        )

    app.add_routes(routes)
    app.router.add_static("/images/", image_dir, show_index=False)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
