"""Lager: Zubehör-Inventar und Lagerorte (Kiste, Regal, Schublade …).

* Lagerorte werden wie Kunden gehandhabt: im Auswahlfenster neu anlegen, später wieder auswählen.
  Zubehör und fertige Drucke (z. B. Vorrat) bekommen einen Lagerort.
* Zubehör und Lagerorte haben eine feste Kennung (A-XXXX bzw. L-XXXX) und eine eigene Seite
  (``item?c=XXXX``, ``loc?c=XXXX``). Die Adressen bleiben gleich, auch wenn der Name sich ändert –
  gedacht für spätere QR-Codes zum schnellen Zuweisen und Verschieben.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .db import CODE_ALPHABET, CODE_LEN, Store, code_label
from .i18n import t, tn
from .ui import e, fmt_date, fmt_input, fmt_num

LOC_PREFIX = "L-"
ACC_PREFIX = "A-"


def parse_item_code(text: str | None) -> tuple[str, str] | None:
    """„L-7K3Q“, „l7k3q“, „A-2HXP“ → ("L", "7K3Q"); ohne Präfix oder unbekannt → None."""
    s = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if len(s) == CODE_LEN + 1 and s[0] in "LA" and all(ch in CODE_ALPHABET for ch in s[1:]):
        return s[0], s[1:]
    return None


# ---- Datenbank ------------------------------------------------------------------

def locations(store: Store) -> list[dict[str, Any]]:
    """Alle Lagerorte mit Anzahl Zubehör und Drucke."""
    rows = store._conn.execute(
        "SELECT l.*, (SELECT COUNT(*) FROM accessories a WHERE a.location_id = l.id) AS n_acc, "
        "(SELECT COUNT(*) FROM jobs j WHERE j.location_id = l.id) AS n_jobs "
        "FROM locations l ORDER BY l.name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def location(store: Store, lid: int) -> dict[str, Any] | None:
    r = store._conn.execute("SELECT * FROM locations WHERE id = ?", (lid,)).fetchone()
    return dict(r) if r else None


def by_code(store: Store, table: str, code: str) -> dict[str, Any] | None:
    r = store._conn.execute(f"SELECT id FROM {table} WHERE code = ?", (code.upper(),)).fetchone()
    if not r:
        return None
    if table == "locations":
        return location(store, r[0])
    from .extras import accessory
    return accessory(store, r[0])


def create_location(store: Store, name: str, note: str | None = None) -> int:
    with store._lock, store._conn:
        cur = store._conn.execute("INSERT INTO locations (code, name, note, created_at) VALUES (?, ?, ?, ?)",
                                  (store.new_item_code("locations"), name.strip()[:80], note, time.time()))
        return int(cur.lastrowid)


def location_by_name(store: Store, name: str) -> int:
    """Lagerort mit diesem Namen (Groß/klein egal) oder neu angelegt."""
    name = name.strip()[:80]
    r = store._conn.execute("SELECT id FROM locations WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return int(r[0]) if r else create_location(store, name)


def save_location(store: Store, lid: int, name: str, note: str | None) -> None:
    with store._lock, store._conn:
        store._conn.execute("UPDATE locations SET name = ?, note = ? WHERE id = ?", (name.strip()[:80], note, lid))


def delete_location(store: Store, lid: int) -> None:
    """Lagerort löschen; Zubehör und Drucke darin sind danach ohne Lagerort."""
    with store._lock, store._conn:
        store._conn.execute("UPDATE accessories SET location_id = NULL WHERE location_id = ?", (lid,))
        store._conn.execute("UPDATE jobs SET location_id = NULL WHERE location_id = ?", (lid,))
        store._conn.execute("DELETE FROM locations WHERE id = ?", (lid,))


def _touch(store: Store, lid: int | None) -> None:
    if lid:
        store._conn.execute("UPDATE locations SET used_at = ? WHERE id = ?", (time.time(), lid))


def assign_jobs(store: Store, ids: list[int], lid: int | None) -> dict[int, int | None]:
    """Lagerort für Drucke setzen; gibt die vorherigen Lagerorte zurück (für „Rückgängig“)."""
    if not ids or (lid and not location(store, lid)):
        return {}
    marks = ", ".join("?" for _ in ids)
    prev = {r[0]: r[1] for r in store._conn.execute(f"SELECT id, location_id FROM jobs WHERE id IN ({marks})", ids)}
    with store._lock, store._conn:
        store._conn.execute(f"UPDATE jobs SET location_id = ? WHERE id IN ({marks})", [lid, *prev])
        _touch(store, lid)
    return prev


def assign_accessories(store: Store, ids: list[int], lid: int | None) -> dict[int, int | None]:
    if not ids or (lid and not location(store, lid)):
        return {}
    marks = ", ".join("?" for _ in ids)
    prev = {r[0]: r[1] for r in store._conn.execute(f"SELECT id, location_id FROM accessories WHERE id IN ({marks})", ids)}
    with store._lock, store._conn:
        store._conn.execute(f"UPDATE accessories SET location_id = ? WHERE id IN ({marks})", [lid, *prev])
        _touch(store, lid)
    return prev


def jobs_at(store: Store, lid: int) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT j.*, c.name AS customer_name, "
        "(SELECT k.image FROM jobs k WHERE k.project_id = j.id AND k.image IS NOT NULL ORDER BY k.start_ts LIMIT 1) AS kid_image "
        "FROM jobs j LEFT JOIN customers c ON c.id = j.customer_id WHERE j.location_id = ? "
        "ORDER BY j.start_ts DESC", (lid,)).fetchall()
    return [dict(r) for r in rows]


def item_uses(store: Store, aid: int) -> list[dict[str, Any]]:
    """Drucke, an denen dieses Zubehör hängt (neueste zuerst)."""
    rows = store._conn.execute(
        "SELECT x.*, j.task_name, j.code, j.start_ts, j.quantity, j.image FROM job_extras x JOIN jobs j ON j.id = x.job_id "
        "WHERE x.accessory_id = ? ORDER BY j.start_ts DESC LIMIT 50", (aid,)).fetchall()
    return [dict(r) for r in rows]


# ---- Auswahlfenster (wie bei Kunden) ------------------------------------------------

def picker_data(store: Store) -> str:
    """Lagerorte für das Auswahlfenster; „Zuletzt“ = zuletzt zugewiesen."""
    from .pages import _ago

    data = [{"id": r["id"], "name": r["name"], "last": r.get("used_at") or 0, "ago": _ago(r["used_at"]) if r.get("used_at") else ""}
            for r in locations(store)]
    raw = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="ldata">{raw}</script>'


def loc_chip(j: dict[str, Any], cls: str = "bdg") -> str:
    """Chip „📦 Lagerort“ an einem Druck: antippen öffnet die Auswahl."""
    attrs = f'data-lid="{j["id"]}" data-lname="{e(j.get("task_name") or "")}"'
    if j.get("location_id") and j.get("location_name"):
        return (f'<span role="button" tabindex="0" class="{cls} kset lc" {attrs} data-cur="{j["location_id"]}" '
                f'title="{e(t("Lagerort ändern"))}">📦 {e(j["location_name"])}</span>')
    return f'<span role="button" tabindex="0" class="kchip lc lnone" {attrs}>📦 {t("＋ Lagerort")}</span>'


def loc_cell(aid: Any, a: dict[str, Any]) -> str:
    """Lagerort im Inventar: Chip mit Auswahlfenster, Wert in versteckten Feldern des Formulars."""
    cur = a.get("location_id") or ""
    txt = f'📦 {e(a["loc_name"])}' if a.get("loc_name") else f'📦 {t("＋ Lagerort")}'
    cls = "kset" if a.get("loc_name") else "kchip lnone"
    return (f'<span role="button" tabindex="0" class="tag {cls} lacc" data-acc="{aid}" data-cur="{cur}" '
            f'data-lname="{e(a.get("name") or t("Neues Zubehör"))}">{txt}</span>'
            f'<input type="hidden" name="acc_loc__{aid}" value="{cur}"><input type="hidden" name="acc_locnew__{aid}" value="">')


LPICK_JS = r"""
// Lagerort-Auswahl: gleiches Fenster wie bei Kunden (mkPick aus KPICK_JS)
const LP=mkPick({data:'ldata',id:'lpop',api:'api/locations/assign',field:'location_id',icon:'📦',
  ph:_t("Lagerort suchen …"),nw:_t("＋ Neuer Lagerort"),none:_t("Ohne Lagerort"),nwph:_t("z. B. Kiste 3, Regal oben"),forp:_t("Lagerort für"),
  all:_t("Alle Lagerorte"),empty:_t("Noch keine Lagerorte."),mk:_t("＋ „{name}“ als neuen Lagerort anlegen")});
function lpChip(ev,done){
  if(document.body.classList.contains('selmode'))return false;
  const c=ev.target.closest('.lc');if(!c)return false;ev.preventDefault();ev.stopPropagation();
  LP.open(c,{ids:[c.dataset.lid],title:c.dataset.lname,cur:c.dataset.cur,done:d=>done&&done(d,c)});return true}
// Inventar: Auswahl landet in versteckten Feldern, gespeichert wird mit dem Formular
document.addEventListener('click',ev=>{const c=ev.target.closest('.lacc');if(!c)return;ev.preventDefault();ev.stopPropagation();
  LP.open(c,{ids:[],title:c.dataset.lname,cur:c.dataset.cur,onpick:fd=>{
    const id=fd.get('location_id'),nn=fd.get('new_name'),a=c.dataset.acc;
    const f1=document.querySelector('input[name="acc_loc__'+a+'"]'),f2=document.querySelector('input[name="acc_locnew__'+a+'"]');
    f1.value=nn?'':(id||'');f2.value=nn||'';c.dataset.cur=f1.value;
    const x=id?LP.custs.find(y=>String(y.id)===String(id)):null,name=nn||(x&&x.name);
    c.textContent='📦 '+(name||_t("＋ Lagerort"));c.className='tag '+(name?'kset':'kchip lnone')+' lacc';
    c.closest('form')&&c.closest('form').classList.add('dirty')}})},true);
document.addEventListener('keydown',ev=>{if((ev.key==='Enter'||ev.key===' ')&&ev.target.matches('.lc,.lacc')){ev.preventDefault();ev.target.click()}});
"""

LPICK_RELOAD_JS = """
document.addEventListener('click',ev=>{lpChip(ev,()=>{const u=new URL(location.href);u.searchParams.set('saved','1');u.hash='';location.replace(u)})},true);
"""

LOC_CSS = """
.lc.kset,.lacc.kset,.amove.kset{border:1px solid var(--line);border-radius:999px;padding:2px 11px;font-weight:700;background:color-mix(in srgb,var(--fg) 5%,transparent)}
.lc.kset:hover,.lacc.kset:hover,.amove.kset:hover{border-color:var(--accent)}
.amove{cursor:pointer;display:inline-flex;white-space:nowrap}.lghead .amove{font-size:14px}
.vloc{display:block;margin-top:5px}.vloc .tag{font-size:12.5px}
.lc.lnone,.lacc.lnone{border-color:color-mix(in srgb,var(--fg) 30%,var(--line));color:var(--muted)}
.lc.lnone:hover,.lacc.lnone:hover{color:var(--accent);border-color:var(--accent)}
.lacc{cursor:pointer;display:inline-flex;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px}
"""


# ---- Seite „Lager“ --------------------------------------------------------------------

STORAGE_CSS = """
.lghead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 12px}.lghead h1{margin:0;font-size:24px}
.lgtabs{display:inline-flex;gap:4px;border:1px solid var(--line);border-radius:14px;padding:4px;background:var(--card);margin:0 0 14px}
.lgtabs a{padding:8px 16px;border-radius:10px;text-decoration:none;font-weight:700;color:var(--muted)}
.lgtabs a.on{background:var(--accent);color:var(--bg)}
.lgbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 10px}.lgbar input[type=search]{flex:1 1 240px;min-width:0}
.lgbar .chs{display:flex;gap:6px;flex-wrap:wrap}.lgbar .chs button{border:1px solid var(--line);background:none;color:var(--muted);border-radius:999px;padding:4px 12px;font-weight:700;cursor:pointer;min-height:0}
.lgbar .chs button.on{color:var(--accent);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent)}
table.acc a.acode{display:inline-block;margin-top:4px;font-size:11.5px;color:var(--muted);text-decoration:none;font-family:ui-monospace,monospace}
table.acc a.acode:hover{color:var(--accent)}
.lgsave{display:flex;gap:12px;align-items:center;margin-top:14px}
.locs{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.locc{border:1px solid var(--line);border-radius:16px;background:var(--card);padding:14px 16px;text-decoration:none;color:var(--fg);display:flex;flex-direction:column;gap:6px}
.locc:hover{border-color:var(--accent)}.locc b{font-size:17px}.locc .cd{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)}
.locc small{color:var(--muted)}.locc .imgs{display:flex;gap:4px;margin-top:4px}.locc .imgs img,.locc .imgs i{width:38px;height:38px;border-radius:8px;object-fit:contain;
 background:color-mix(in srgb,var(--fg) 5%,var(--bg));font-style:normal;display:inline-flex;align-items:center;justify-content:center;font-size:20px}
.locnew{border:1.5px dashed var(--line);border-radius:16px;padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.locnew input{width:100%;box-sizing:border-box}
.lgx{display:grid;gap:2px}.lgx .r{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:12px;align-items:center;padding:8px 4px;border-bottom:1px solid var(--line)}
.lgx .r:last-child{border-bottom:0}.lgx .r .ic{font-size:24px;text-align:center}.lgx .r img,.lgx .r .ph{width:44px;height:44px;border-radius:8px;object-fit:contain;background:color-mix(in srgb,var(--fg) 5%,var(--bg))}
.lgx .r b a{color:var(--fg);text-decoration:none}.lgx .r b a:hover{color:var(--accent)}.lgx .r small{display:block;color:var(--muted);font-size:12.5px}
.lgx .r .tag{font-size:12.5px}
.lgcard{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 18px;margin-bottom:16px}
.lgcard h2{margin-top:0}
.idbox{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
.idbox code{font-size:15px;font-weight:800;color:var(--fg);background:color-mix(in srgb,var(--fg) 7%,transparent);border-radius:8px;padding:3px 10px}
.idbox button{min-height:0;padding:4px 10px;font-size:12.5px}
.lgform{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:10px;align-items:end}
.lgform label{display:grid;gap:5px;font-size:13px;font-weight:700;color:var(--muted)}
.lgform .row{grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.ifacts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:0 0 14px}
.ifacts div{border:1px solid var(--line);border-radius:14px;background:var(--card);padding:10px 14px}
.ifacts span{display:block;font-size:12px;color:var(--muted);font-weight:600}.ifacts b{font-size:18px}
.ifacts .lowv b{color:var(--warn)}.ifacts .outv b{color:var(--bad)}
.lgform.b3{grid-template-columns:repeat(3,minmax(0,180px))}.lgform.b3 input{width:100%;box-sizing:border-box}
@media (max-width:700px){
 table.acc tr{grid-template-columns:repeat(6,minmax(0,1fr))!important}
 table.acc td:nth-child(1){grid-column:span 1}table.acc td:nth-child(1):before,table.acc td:nth-child(9):before{display:none}
 table.acc td:nth-child(2){grid-column:span 5}table.acc td:nth-child(3),table.acc td:nth-child(4),table.acc td:nth-child(5),table.acc td:nth-child(6){grid-column:span 3}
 table.acc td:nth-child(7){grid-column:span 2}table.acc td:nth-child(8){grid-column:span 3}table.acc td:nth-child(9){grid-column:span 1;align-self:end}
 table.acc td.nm{min-width:0}table.acc .pk input{width:auto;flex:1 1 0;min-width:0}table.acc .zg input{width:auto;flex:1 1 0;min-width:0}
 table.acc input.num{width:100%}table.acc .lacc{max-width:100%}
 .lgbar .chs{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;max-width:100%}.lgbar .chs button{white-space:nowrap}
 .kpis{grid-template-columns:repeat(2,minmax(0,1fr))!important}}
@media (max-width:700px){.lgform.b3{grid-template-columns:1fr 1fr}.lgform{grid-template-columns:1fr}.lgtabs{display:flex}.lgtabs a{flex:1;text-align:center}}
"""

TABS_JS = """
document.querySelectorAll('.lgbar').forEach(bar=>{const s=bar.querySelector('input[type=search]'),rows=[...document.querySelectorAll('table.acc tbody tr:not(.new)')];
  let g=null,l=null;
  function apply(){const q=(s.value||'').trim().toLowerCase();rows.forEach(r=>{r.hidden=!!((q&&!r.dataset.n.includes(q))||(g!==null&&r.dataset.g!==g)||(l!==null&&r.dataset.l!==l))})}
  s.addEventListener('input',apply);
  bar.querySelectorAll('.chs').forEach(ch=>ch.addEventListener('click',ev=>{const b=ev.target.closest('button');if(!b)return;
    ch.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
    const v=b.dataset.v===undefined?null:b.dataset.v;if(ch.dataset.k==='g')g=v;else l=v;apply()}))});
document.querySelectorAll('a.abs').forEach(a=>{const u=new URL(a.href);a.textContent=u.pathname+u.search});
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',()=>{try{navigator.clipboard.writeText(b.dataset.copy);
  const o=b.textContent;b.textContent=_t("Kopiert");setTimeout(()=>b.textContent=o,1200)}catch(_){}}));
"""


def _kpis(store: Store, accs: list[dict[str, Any]], locs: list[dict[str, Any]]) -> str:
    from .extras import low_stock

    value = sum(max(a.get("stock") or 0, 0) * (a.get("unit_price") or 0) for a in accs)
    low = low_stock(store)
    return (f'<div class="kpis"><div class="kpi"><span>🧰 {t("Zubehör")}</span><b>{tn(len(accs), "{n} Artikel", "{n} Artikel")}</b>'
            f'<small>{t("{n} Gruppen", n=len({a["grp"] for a in accs if a.get("grp")}))}</small></div>'
            f'<div class="kpi run"><span>{t("Lagerwert Zubehör")}</span><b class="calc">{fmt_num(value, 2, "€")}</b><small>{t("Bestand × Stückpreis")}</small></div>'
            f'<div class="kpi"><span>{t("Bald nachkaufen")}</span><b>{len(low)}</b><small>{e(", ".join(a["name"] for a in low[:3]))}</small></div>'
            f'<div class="kpi"><span>📦 {t("Lagerorte")}</span><b>{len(locs)}</b><small>{t("{n} Drucke eingelagert", n=sum(r["n_jobs"] for r in locs))}</small></div></div>')


def page_storage(store: Store, tab: str | None, saved: str | None) -> str:
    from .extras import SETTINGS_CSS, accessories, inventory_rows
    from .pages import KPICK_CSS, KPICK_JS
    from .ui import layout

    accs = accessories(store)
    locs = locations(store)
    tab = "orte" if tab == "orte" else "zubehoer"
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    tabs = (f'<nav class="lgtabs"><a href="storage" class="{"on" if tab == "zubehoer" else ""}">🧰 {t("Zubehör")}</a>'
            f'<a href="storage?tab=orte" class="{"on" if tab == "orte" else ""}">📦 {t("Lagerorte")}</a></nav>')
    if tab == "zubehoer":
        groups = sorted({a["grp"] for a in accs if a.get("grp")}, key=str.casefold)
        gch = (f'<span class="chs" data-k="g"><button type="button" class="on">{t("Alle Gruppen")}</button>'
               + "".join(f'<button type="button" data-v="{e(g)}">{e(g)}</button>' for g in groups) + '</span>') if groups else ""
        used = [r for r in locs if r["n_acc"]]
        lch = (f'<span class="chs" data-k="l"><button type="button" class="on">{t("Alle Orte")}</button>'
               + "".join(f'<button type="button" data-v="{r["id"]}">📦 {e(r["name"])}</button>' for r in used)
               + f'<button type="button" data-v="">{t("ohne Ort")}</button></span>') if used else ""
        content = f"""<form method="post" action="storage" class="settings">
<div class="lgbar"><input type="search" placeholder="{e(t("Zubehör suchen …"))}" autocomplete="off">{gch}{lch}</div>
<p class="muted">{t("Preis pro Stück = Packungspreis ÷ Menge. Zugeordnetes Zubehör ist reserviert, vom Bestand abgebucht wird es beim Montieren (oder bei der Übergabe). Zugang = neue Packung gekauft: Menge und Preis eintragen, der Stückpreis wird gemittelt.")}</p>
{inventory_rows(store)}
<div class="lgsave"><button class="btn primary">{t("Speichern")}</button><span class="muted">{t("Preise gelten danach für alle Drucke (werden neu berechnet).")}</span></div></form>"""
    else:
        cards = ""
        for r in locs:
            shown = jobs_at(store, r["id"])[:4]
            imgs = "".join(f'<img src="images/{e(j.get("image") or j.get("kid_image"))}" alt="" loading="lazy">'
                           if j.get("image") or j.get("kid_image") else "<i>🧩</i>" for j in shown)
            icons = [a.get("icon") or "🔹" for a in accs if a.get("location_id") == r["id"]][:6 - len(shown)]
            imgs += "".join(f"<i>{e(i)}</i>" for i in icons)
            sub = " · ".join(x for x in (tn(r["n_acc"], "{n} Zubehör", "{n} Zubehör") if r["n_acc"] else "",
                                         tn(r["n_jobs"], "{n} Druck", "{n} Drucke") if r["n_jobs"] else "") if x) or t("leer")
            cards += (f'<a class="locc" href="loc?c={e(r["code"])}"><b>📦 {e(r["name"])}</b><span class="cd">{LOC_PREFIX}{e(r["code"])}</span>'
                      + (f'<small>{e(r["note"])}</small>' if r.get("note") else "")
                      + f'<small>{e(sub)}</small>' + (f'<span class="imgs">{imgs}</span>' if imgs else "") + '</a>')
        cards += (f'<form class="locnew" method="post" action="storage/locations"><b>＋ {t("Neuer Lagerort")}</b>'
                  f'<input name="name" maxlength="80" required placeholder="{e(t("z. B. Kiste 3, Regal oben"))}">'
                  f'<input name="note" maxlength="200" placeholder="{e(t("Notiz (optional)"))}">'
                  f'<button class="btn primary">{t("Anlegen")}</button></form>')
        content = (f'<p class="muted">{t("Lagerorte gelten für Zubehör und für fertige Drucke (z. B. Vorrat). Zuweisen geht über den Chip „📦 Lagerort“ im Inventar, auf der Detailseite eines Drucks, in der Ansicht „Vorrat“ oder für mehrere Drucke über „Auswählen“. Jeder Lagerort hat eine feste Kennung (L-…) und eine eigene Seite.")}</p>'
                   f'<div class="locs">{cards}</div>')
    body = (f'<style>{SETTINGS_CSS}{KPICK_CSS}{LOC_CSS}{STORAGE_CSS}</style>{banner}'
            f'<div class="lghead"><h1>{t("Lager")}</h1></div>{_kpis(store, accs, locs)}{tabs}{content}{picker_data(store)}')
    return layout(t("Lager"), "storage", body, KPICK_JS + LPICK_JS + TABS_JS)


def _id_box(prefix: str, code: str, kind: str) -> str:
    link = f"{kind}?c={code}"
    return (f'<div class="idbox">{t("Kennung")} <code>{prefix}{e(code)}</code>'
            f'<button type="button" class="btn" data-copy="{prefix}{e(code)}">{t("Kopieren")}</button>'
            f'<span>{t("Feste Adresse: {link} (bleibt gleich, auch wenn sich der Name ändert)", link=f"<a class=abs href={link}>{link}</a>")}</span></div>')


def page_location(store: Store, loc: dict[str, Any], saved: str | None) -> str:
    from .extras import SETTINGS_CSS, accessories
    from .pages import KPICK_CSS, KPICK_JS
    from .ui import layout

    lid = loc["id"]
    accs = [a for a in accessories(store) if a.get("location_id") == lid]
    jobs = jobs_at(store, lid)
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    arows = "".join(
        f'<div class="r"><span class="ic">{e(a.get("icon") or "🔹")}</span><div><b><a href="item?c={e(a["code"])}">{e(a["name"])}</a></b>'
        f'<small>{t("{n} auf Lager", n=fmt_input(round(a.get("stock") or 0, 2)))}{" · " + e(a["grp"]) if a.get("grp") else ""}</small></div>'
        f'<span>{_acc_move(a)}</span></div>' for a in accs)
    jrows = ""
    for j in jobs:
        src = j.get("image") or j.get("kid_image")
        pic = f'<img src="images/{e(src)}" alt="" loading="lazy">' if src else '<div class="ph"></div>'
        sub = [fmt_date(j.get("start_ts"), "date") if j.get("start_ts") else "", code_label(j.get("code"))]
        if j.get("customer_name"):
            sub.append("👤 " + j["customer_name"])
        if (j.get("quantity") or 1) > 1:
            sub.append(t("{n} Stk.", n=j["quantity"]))
        jrows += (f'<div class="r">{pic}<div><b><a href="job?id={j["id"]}">{"📦 " if j.get("kind") == "project" else ""}{e(j.get("task_name") or "–")}</a></b>'
                  f'<small>{e(" · ".join(x for x in sub if x))}</small></div><span>{loc_chip({**j, "location_name": loc["name"]}, "tag")}</span></div>')
    body = f"""<style>{SETTINGS_CSS}{KPICK_CSS}{LOC_CSS}{STORAGE_CSS}</style>{banner}
<p><a href="storage?tab=orte">← {t("Lagerorte")}</a></p>
<div class="lghead"><h1>📦 {e(loc["name"])}</h1></div>
<div class="lgcard">{_id_box(LOC_PREFIX, loc["code"], "loc")}
<form method="post" action="storage/locations" class="lgform" style="margin-top:12px"><input type="hidden" name="id" value="{lid}">
<label>{t("Name")}<input name="name" value="{e(loc["name"])}" maxlength="80" required></label>
<label>{t("Notiz")}<input name="note" value="{e(loc.get("note") or "")}" maxlength="200" placeholder="{e(t("z. B. Keller, linkes Regal"))}"></label>
<div class="row"><button class="btn primary">{t("Speichern")}</button><span class="sp" style="flex:1"></span>
<button class="btn danger" name="delete" value="1" formnovalidate onclick="return confirm({e(json.dumps(t("Lagerort löschen? Zubehör und Drucke darin sind danach ohne Lagerort.")))})">🗑 {t("Löschen")}</button></div></form></div>
<div class="lgcard"><h2>🧰 {t("Zubehör hier")} <small class="muted">({len(accs)})</small></h2>
{f'<div class="lgx">{arows}</div>' if arows else f'<p class="muted">{t("Kein Zubehör an diesem Ort.")}</p>'}</div>
<div class="lgcard"><h2>🧩 {t("Drucke hier")} <small class="muted">({len(jobs)})</small></h2>
{f'<div class="lgx">{jrows}</div>' if jrows else f'<p class="muted">{t("Keine Drucke an diesem Ort. Zuweisen über den Chip „📦 Lagerort“ auf der Detailseite oder in der Ansicht „Vorrat“.")}</p>'}</div>
{picker_data(store)}"""
    return layout(loc["name"], "storage", body, KPICK_JS + LPICK_JS + LPICK_RELOAD_JS + MOVE_JS + TABS_JS)


def _acc_move(a: dict[str, Any]) -> str:
    """Chip zum Verschieben eines Zubehörs (speichert sofort)."""
    cur = a.get("location_id") or ""
    txt = f'📦 {e(a["loc_name"])}' if a.get("loc_name") else f'📦 {t("＋ Lagerort")}'
    cls = "kset" if a.get("loc_name") else "kchip lnone"
    return (f'<span role="button" tabindex="0" class="tag {cls} amove" data-aid="{a["id"]}" data-cur="{cur}" '
            f'data-lname="{e(a.get("name") or "")}">{txt}</span>')


MOVE_JS = """
// Zubehör verschieben: speichert sofort und lädt neu
document.addEventListener('click',ev=>{const c=ev.target.closest('.amove');if(!c)return;ev.preventDefault();ev.stopPropagation();
  LP.open(c,{ids:[],title:c.dataset.lname,cur:c.dataset.cur,onpick:async fd=>{fd.append('acc',c.dataset.aid);
    await fetch('api/locations/assign',{method:'POST',body:fd});const u=new URL(location.href);u.searchParams.set('saved','1');location.replace(u)}})},true);
"""


def page_item(store: Store, a: dict[str, Any], saved: str | None) -> str:
    from .extras import SETTINGS_CSS, need
    from .pages import KPICK_CSS, KPICK_JS
    from .ui import layout

    aid = a["id"]
    res = sum(max(need(x, x.get("quantity") or 1) - (x.get("used") or 0), 0) for x in item_uses(store, aid))
    stock = a.get("stock") or 0
    free = stock - res
    cls = "outv" if stock <= 0 else "lowv" if a.get("warn_at") is not None and free <= a["warn_at"] else ""
    banner = f'<div class="banner">{t("Gespeichert.")}</div>' if saved else ""
    buy = (f' · <a href="{e(a["url"])}" target="_blank" rel="noopener">🛒 {t("Kauflink öffnen")}</a>'
           if a.get("url") and str(a["url"]).startswith(("http://", "https://")) else "")
    facts = (f'<div class="ifacts"><div class="{cls}"><span>{t("Bestand")}</span><b>{fmt_input(round(stock, 2))}</b></div>'
             f'<div><span>{t("reserviert")}</span><b>{fmt_input(round(res, 2))}</b></div>'
             f'<div class="{cls}"><span>{t("frei")}</span><b>{fmt_input(round(free, 2))}</b></div>'
             f'<div><span>{t("Preis pro Stück")}</span><b>{fmt_num(a.get("unit_price"), 3, "€")}</b></div>'
             f'<div><span>{t("Warnen ab")}</span><b>{fmt_input(a.get("warn_at")) or "–"}</b></div></div>')
    uses = ""
    for x in item_uses(store, aid)[:20]:
        pic = f'<img src="images/{e(x["image"])}" alt="" loading="lazy">' if x.get("image") else '<div class="ph"></div>'
        n = need(x, x.get("quantity") or 1)
        uses += (f'<div class="r">{pic}<div><b><a href="job?id={x["job_id"]}">{e(x.get("task_name") or "–")}</a></b>'
                 f'<small>{e(fmt_date(x.get("start_ts"), "date"))} · {e(code_label(x.get("code")))}</small></div>'
                 f'<span>{t("{n} Stk.", n=fmt_input(round(n, 2)))}{" ✓" if (x.get("used") or 0) >= n - 1e-9 else ""}</span></div>')
    body = f"""<style>{SETTINGS_CSS}{KPICK_CSS}{LOC_CSS}{STORAGE_CSS}</style>{banner}
<p><a href="storage">← {t("Lager")}</a></p>
<div class="lghead"><h1>{e(a.get("icon") or "🔹")} {e(a["name"])}</h1>{_acc_move(a)}</div>
<p class="muted">{e(a.get("grp") or "")}{buy}</p>
{facts}
<div class="lgcard">{_id_box(ACC_PREFIX, a["code"], "item")}
<form method="post" action="storage/item" class="lgform b3" style="margin-top:12px"><input type="hidden" name="id" value="{aid}">
<label>{t("Zugang (Stk.)")}<input class="num" name="aq" inputmode="decimal" placeholder="100"></label>
<label>{t("Preis der Packung €")}<input class="num" name="ap" inputmode="decimal" placeholder="6,99"></label>
<label>{t("Entnehmen (Stk.)")}<input class="num" name="take" inputmode="decimal" placeholder="1"></label>
<div class="row"><button class="btn primary">{t("Buchen")}</button><a href="storage#a{aid}">{t("Im Inventar bearbeiten")} →</a></div></form></div>
<div class="lgcard"><h2>🧩 {t("Verwendet in")}</h2>{f'<div class="lgx">{uses}</div>' if uses else f'<p class="muted">{t("Noch an keinem Druck.")}</p>'}</div>
{picker_data(store)}"""
    return layout(a["name"], "storage", body, KPICK_JS + LPICK_JS + MOVE_JS + TABS_JS)


def item_booking(store: Store, aid: int, aq: float | None, ap: float | None, take: float | None) -> None:
    """Zugang (neue Packung) oder Entnahme ohne Druck (z. B. kaputt, anderweitig verwendet)."""
    from .extras import accessory, restock, save_accessory

    if aq and aq > 0:
        restock(store, aid, aq, ap)
    if take and take > 0:
        a = accessory(store, aid)
        if a:
            save_accessory(store, aid, stock=(a.get("stock") or 0) - take)
