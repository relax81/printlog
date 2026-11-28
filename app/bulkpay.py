"""Zahlung für mehrere Posten: ein Betrag eines Kunden wird auf seine offenen Posten verteilt.

Aufruf aus der Ansicht „Offen“ (Knopf im Kundenkopf), von der Kundenseite und aus der Mehrfachauswahl.
Im Dialog wählt man die Posten, gibt den Betrag ein und sieht sofort, wer wie viel bekommt. Jeder Posten
bekommt eine normale Teilzahlung. Zu viel Gezahltes geht als Trinkgeld an den neuesten Posten, wird anteilig
verteilt oder als Guthaben beim Kunden gemerkt; ein Guthaben lässt sich beim nächsten Mal mitverwenden.
"""

from __future__ import annotations

from typing import Any

from .db import Store, code_label
from .i18n import t, tn
from .pages import _open_state, amount_due
from .ui import e, fmt_date, fmt_num

MODES = ("oldest", "share")
SURPLUS = ("tip", "spread", "credit")


def _cents(v: float | None) -> int:
    return round((v or 0) * 100)


def _share(total: int, weights: list[int]) -> list[int]:
    """``total`` Cent im Verhältnis von ``weights`` aufteilen; Rundungsreste gehen an die ersten Posten."""
    s = sum(weights)
    if not weights or s <= 0 or total <= 0:
        return [0] * len(weights)
    out = [total * w // s for w in weights]
    rest = total - sum(out)
    i = 0
    while rest > 0:
        if weights[i % len(weights)] > 0:
            out[i % len(weights)] += 1
            rest -= 1
        i += 1
    return out


def allocate(opens: list[int], amount: int, mode: str = "oldest", surplus: str = "tip") -> tuple[list[int], int]:
    """Verteilt ``amount`` Cent auf Posten mit offenen Beträgen ``opens`` (älteste zuerst).

    Gibt (Cent je Posten, Cent fürs Guthaben) zurück. Reicht der Betrag nicht, bekommen bei „oldest“ die
    ältesten Posten alles und einer den Rest, bei „share“ jeder seinen Anteil. Zu viel Gezahltes: „tip“ an
    den neuesten Posten, „spread“ anteilig auf alle, „credit“ als Guthaben.
    """
    total = sum(opens)
    if amount <= 0 or not opens:
        return [0] * len(opens), 0
    if amount >= total:
        give, extra = list(opens), amount - total
    elif mode == "share":
        give, extra = _share(amount, opens), 0
    else:
        give, left = [], amount
        for o in opens:
            g = min(o, left)
            give.append(g)
            left -= g
        extra = 0
    credit = 0
    if extra > 0:
        if surplus == "credit":
            credit = extra
        elif surplus == "spread" and total > 0:
            give = [g + x for g, x in zip(give, _share(extra, opens))]
        else:
            give[-1] += extra
    return give, credit


def open_items(store: Store, customer_id: int | None = None) -> list[dict[str, Any]]:
    """Posten mit offenem Betrag (wie in „Offen“), älteste zuerst."""
    jobs = store.list_jobs(pending="1", **({"customer": str(customer_id)} if customer_id else {}))
    items = [j for j in jobs if _open_state(j)[0]]
    items.sort(key=lambda j: (j.get("start_ts") or 0, j["id"]))
    return items


def _item(j: dict[str, Any], kids: dict[int, list[dict[str, Any]]], ids: set[int] | None) -> dict[str, Any]:
    due = amount_due(j) or 0
    paid = j.get("paid") or 0
    sub = [fmt_date(j["start_ts"], "dm")] if j.get("start_ts") else []
    if j.get("kind") == "project":
        sub.append("📦 " + tn(len(kids.get(j["id"], [])), "{n} Druck", "{n} Drucke"))
    if j.get("kind") == "share":
        sub.append(t("✂ Anteil"))
    if paid > 0.005:
        sub.append(t("{v} schon bezahlt", v=fmt_num(paid, 2, "€")))
    link = j["share_of"] if j.get("kind") == "share" and j.get("share_of") else j["id"]
    return {"id": j["id"], "name": j.get("task_name") or t("Druck"), "code": code_label(j.get("code")) if j.get("code") else "",
            "sub": " · ".join(sub), "open": max(0, _cents(due) - _cents(paid)), "href": f"job?id={link}",
            "on": ids is None or j["id"] in ids}


def dialog_data(store: Store, customer_id: int | None, picked: list[int] | None) -> dict[str, Any]:
    """Daten für den Dialog: Kunde, Guthaben und offene Posten (ausgewählte angehakt).

    Mit ``picked`` (Mehrfachauswahl) zählen ausgewählte Drucke, ihre Projekte und Anteile aufgeteilter Drucke.
    """
    ids: set[int] | None = None
    if picked is not None:
        targets: set[int] = set()
        for jid in picked:
            j = store.get(jid)
            if not j:
                continue
            targets.add(jid)
            if j.get("project_id"):
                targets.add(j["project_id"])
        hits = [j for j in open_items(store) if j["id"] in targets or (j.get("kind") == "share" and j.get("share_of") in targets)]
        custs = {j["customer_id"] for j in hits}
        if not hits:
            return {"error": t("In der Auswahl ist nichts offen.")}
        if len(custs) > 1:
            return {"error": t("Die Auswahl gehört zu mehreren Kunden. Bitte nur Drucke eines Kunden wählen.")}
        customer_id = custs.pop()
        ids = {j["id"] for j in hits}
    c = store.customer(customer_id) if customer_id else None
    if not c:
        return {"error": t("Kunde nicht gefunden.")}
    items = open_items(store, customer_id)
    kids = store.children([j["id"] for j in items if j.get("kind") == "project"])
    return {"customer": {"id": c["id"], "name": c["name"]}, "credit": _cents(store.credit_balance(c["id"])),
            "items": [_item(j, kids, ids) for j in items]}


def book(store: Store, customer_id: int, ids: list[int], amount: float, mode: str, surplus: str,
         use_credit: bool, ts: float | None, note: str | None) -> dict[str, Any]:
    """Bucht den Betrag; gibt die neuen Zahlungs- und Guthaben-IDs (zum Rückgängigmachen) und eine Übersicht zurück."""
    mode = mode if mode in MODES else "oldest"
    surplus = surplus if surplus in SURPLUS else "tip"
    want = set(ids)
    items = [j for j in open_items(store, customer_id) if j["id"] in want]
    if not items:
        return {"error": t("Keine offenen Posten ausgewählt.")}
    opens = [max(0, _cents(amount_due(j)) - _cents(j.get("paid"))) for j in items]
    cash = max(0, _cents(amount))
    balance = max(0, _cents(store.credit_balance(customer_id))) if use_credit else 0
    used = min(balance, max(0, sum(opens) - cash))
    if cash + used <= 0:
        return {"error": t("Bitte einen Betrag eingeben.")}
    give, credit = allocate(opens, cash + used, mode, surplus)
    pay_note = note
    if used:
        extra = t("inkl. Guthaben")
        pay_note = f"{note} · {extra}" if note else extra
    pay_ids = [store.add_payment_id(j["id"], g / 100, ts, pay_note) for j, g in zip(items, give) if g > 0]
    credit_ids = []
    if used:
        credit_ids.append(store.add_credit(customer_id, -used / 100, ts,
                                           t("verwendet für {n} Posten", n=sum(1 for g in give if g > 0))))
    if credit:
        credit_ids.append(store.add_credit(customer_id, credit / 100, ts, note or t("zu viel gezahlt")))
    return {"ok": True, "payments": pay_ids, "credits": credit_ids, "n": len(pay_ids), "credit": credit, "used": used,
            "full": sum(1 for o, g in zip(opens, give) if g >= o)}


def undo(store: Store, payment_ids: list[int], credit_ids: list[int]) -> None:
    for pid in payment_ids:
        store.delete_payment(pid)
    for cid in credit_ids:
        store.delete_credit(cid)


def open_button(customer_id: int, cls: str = "btn pay") -> str:
    """Knopf, der den Dialog für alle offenen Posten des Kunden öffnet."""
    return (f'<button type="button" class="{cls}" data-bulkpay="{customer_id}" title="{e(t("Einen Betrag auf mehrere offene Posten verteilen"))}">'
            f'💶 {t("Zahlung für alle")}</button>')


def dialog() -> str:
    """Leerer Dialog; BULKPAY_JS füllt ihn mit den Daten aus api/bulkpay."""
    return f"""<div class="bpbg" id="bpdlg" hidden><form class="bpd" autocomplete="off">
<h3 class="ttl"></h3><p class="sub"></p>
<div class="bpe" hidden></div>
<div class="bpbody">
<div class="bpfr"><label>{t("Betrag")}<input name="amount" inputmode="decimal" class="amt"></label>
<label>{t("Datum")}<input type="date" name="date"></label></div>
<div class="bpm"><span>{t("Verteilen")}:</span><span class="bpseg"><button type="button" data-m="oldest" class="on">{t("Älteste zuerst")}</button><button type="button" data-m="share">{t("Nach Anteil")}</button></span>
<span class="sp"></span><button type="button" class="lnk tog"></button></div>
<div class="bpl"></div><button type="button" class="lnk more" hidden></button>
<label class="bpcr" hidden><input type="checkbox" name="use_credit" value="1" checked> <span></span></label>
<div class="bpx" hidden><b></b> {t("Was soll damit passieren?")}
<label><input type="radio" name="surplus" value="tip" checked> {t("Als Trinkgeld zum neuesten Posten (zählt dort als tatsächlicher Preis)")}</label>
<label><input type="radio" name="surplus" value="spread"> {t("Anteilig auf alle Posten verteilen")}</label>
<label><input type="radio" name="surplus" value="credit"> <span class="crl"></span></label></div>
<div class="bps" hidden></div>
<label class="bpn">{t("Notiz")}<input name="note" placeholder="{e(t("z. B. bar, für alles"))}"></label>
<div class="ft"><span class="sp"></span><button type="button" class="cancel">{t("Abbrechen")}</button><button type="submit" class="btn go"></button></div>
</div>
<div class="bpok" hidden><p></p><div class="ft"><span class="sp"></span><button type="button" class="undo">{t("Rückgängig")}</button><button type="button" class="btn close">{t("Fertig")}</button></div></div>
</form></div>"""


CSS = """
.bpbg{position:fixed;inset:0;height:100%;height:100dvh;background:#000a;z-index:60;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
.bpbg[hidden]{display:none}
.bpd{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:0 30px 80px #000c;width:min(620px,100%);padding:20px 22px;
 max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);overflow:auto;overscroll-behavior:contain;box-sizing:border-box}
.bpd h3{margin:0 0 2px;font-size:20px}.bpd .sub{color:var(--muted);font-size:14px;margin:0 0 14px}
.bpd [hidden]{display:none!important}
.bpd label{display:flex;flex-direction:column;gap:5px;font-size:13px;font-weight:600;color:var(--muted)}
.bpd label input:not([type=checkbox]):not([type=radio]){width:100%;box-sizing:border-box}
.bpfr{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.bpfr .amt{font-size:18px;font-weight:700;border-color:var(--accent)}
.bpm{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px;color:var(--muted);margin-bottom:8px}.bpm .sp{flex:1}
.bpseg{display:inline-flex;border:1px solid var(--line);border-radius:99px;overflow:hidden}
.bpseg button{border:0;border-radius:0;background:none;padding:5px 12px;min-height:0;font-weight:700;color:var(--muted);cursor:pointer;font-size:14px}
.bpseg button.on{background:var(--accent);color:var(--bg)}
.bpd .lnk{border:0;background:none;padding:2px 4px;min-height:0;color:var(--accent);font-weight:600;cursor:pointer;font-size:13px}
.bpl{border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:300px;margin-bottom:12px}.bpd .more{display:block;margin:-4px 0 12px}
.bpl label{display:grid;grid-template-columns:22px 1fr auto auto;gap:4px 12px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);
 font-size:14px;font-weight:400;color:var(--fg);cursor:pointer}
.bpl label:last-child{border-bottom:0}.bpl label[hidden]{display:none}.bpl label.off{opacity:.55}
.bpl input{width:18px;height:18px;margin:0;accent-color:var(--accent)}
.bpl .nm{min-width:0}.bpl .nm b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bpl .nm small,.bpl .gv small{display:block;color:var(--muted);font-size:12px}
.bpl .op{text-align:right;color:var(--muted);white-space:nowrap}.bpl .gv{text-align:right;min-width:86px;white-space:nowrap}
.bpl .gv b{font-size:15px}.bpl .gv small.ok{color:var(--ok)}.bpl .gv small.part{color:var(--warn)}
.bpl .pid{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted);margin-left:6px;font-weight:600}
.bpcr{flex-direction:row!important;align-items:center;gap:8px!important;font-size:14px!important;color:var(--fg)!important;margin-bottom:10px}
.bpx,.bps{border-radius:12px;padding:10px 12px;margin-bottom:12px;font-size:14px}
.bpx{background:color-mix(in srgb,var(--warn) 14%,transparent);border:1px solid color-mix(in srgb,var(--warn) 45%,transparent)}
.bpx label{flex-direction:row;align-items:flex-start;gap:8px;font-weight:400;color:var(--fg);font-size:14px;margin-top:6px}
.bpx input{margin:3px 0 0;accent-color:var(--accent)}
.bps{background:var(--bg);border:1px solid var(--line);color:var(--muted)}
.bpn{margin-bottom:14px}
.bpd .ft{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.bpd .ft .sp{flex:1}
.bpd .go{background:var(--accent);color:var(--bg);border-color:var(--accent);font-weight:700}.bpd .go:disabled{opacity:.4}
.bpe{color:var(--bad);font-size:14px;margin-bottom:12px}
.bpok p{font-size:15px;margin:4px 0 16px}
@media (max-width:560px){.bpd{padding:16px}.bpm{flex-wrap:nowrap}.bpm>span:first-child{display:none}.bpseg button{padding:5px 10px}.bpl label{grid-template-columns:22px 1fr auto}.bpl .op{display:none}}
"""

BULKPAY_JS = r"""
(()=>{const bg=document.getElementById('bpdlg');if(!bg)return;const f=bg.querySelector('form'),L=bg.querySelector('.bpl');
let D=null,mode='oldest',typed=false,last=null;
const lang=document.documentElement.lang||'de';
const eur=c=>(c/100).toLocaleString(lang,{style:'currency',currency:'EUR'});
const parse=v=>{v=String(v||'').replace(/[€\s]/g,'');if(!v)return 0;
  if(lang==='en'){v=v.replace(/,(?=\d{3}(\D|$))/g,'');if(v.includes(',')&&!v.includes('.'))v=v.replace(',','.')}
  else v=v.replace(/\.(?=\d{3}(\D|$))/g,'').replace(',','.');
  const n=parseFloat(v);return isNaN(n)||n<0?0:Math.round(n*100)};
const fmtIn=c=>(c/100).toLocaleString(lang,{minimumFractionDigits:2,maximumFractionDigits:2,useGrouping:false});
function share(total,w){const s=w.reduce((a,b)=>a+b,0);if(!w.length||s<=0||total<=0)return w.map(()=>0);
  const o=w.map(x=>Math.floor(total*x/s));let r=total-o.reduce((a,b)=>a+b,0),i=0;
  while(r>0){if(w[i%w.length]>0){o[i%w.length]++;r--}i++}return o}
function allocate(opens,amount,surplus){const tot=opens.reduce((a,b)=>a+b,0);if(amount<=0||!opens.length)return[opens.map(()=>0),0];
  let give,extra=0;if(amount>=tot){give=opens.slice();extra=amount-tot}
  else if(mode==='share')give=share(amount,opens);
  else{let l=amount;give=opens.map(o=>{const g=Math.min(o,l);l-=g;return g})}
  let credit=0;if(extra>0){if(surplus==='credit')credit=extra;else if(surplus==='spread')give=give.map((g,i)=>g+share(extra,opens)[i]);else give[give.length-1]+=extra}
  return[give,credit]}
const boxes=()=>[...L.querySelectorAll('input[type=checkbox]')];
function sel(){return D.items.filter((_,i)=>boxes()[i].checked)}
function upd(){if(!D)return;const s=sel(),opens=s.map(x=>x.open),tot=opens.reduce((a,b)=>a+b,0);
  const amt=f.amount;if(!typed)amt.value=tot?fmtIn(tot):'';
  const cash=parse(amt.value),useCr=D.credit>0&&f.use_credit.checked,used=useCr?Math.min(D.credit,Math.max(0,tot-cash)):0;
  const sur=(f.querySelector('input[name=surplus]:checked')||{}).value||'tip';
  const [give,credit]=allocate(opens,cash+used,sur);
  let k=0;D.items.forEach((x,i)=>{const lb=L.children[i],gv=lb.querySelector('.gv');lb.classList.toggle('off',!boxes()[i].checked);
    if(!boxes()[i].checked){gv.innerHTML='<b>–</b>';return}const g=give[k++];
    gv.innerHTML='<b>'+eur(g)+'</b>'+(g>=x.open?'<small class="ok">✓ '+_t("bezahlt")+'</small>':g>0?'<small class="part">'+_t("{v} bleibt offen").replace('{v}',eur(x.open-g))+'</small>':'<small>'+_t("bleibt offen")+'</small>')});
  f.querySelector('.sub').textContent=(s.length==1?_t("1 Posten ausgewählt"):_t("{n} Posten ausgewählt").replace('{n}',s.length))+' · '+_t("{v} offen").replace('{v}',eur(tot));
  const cr=f.querySelector('.bpcr');cr.hidden=!(D.credit>0);cr.querySelector('span').textContent=_t("Guthaben verwenden ({v} vorhanden)").replace('{v}',eur(D.credit));
  const over=cash-tot,x=f.querySelector('.bpx');x.hidden=!(over>0&&s.length);
  x.querySelector('b').textContent=_t("{v} mehr als offen.").replace('{v}',eur(Math.max(0,over)));
  x.querySelector('.crl').textContent=_t("Als Guthaben bei {name} merken").replace('{name}',D.customer.name);
  const ps=f.querySelector('.bps'),short=tot-cash-used;ps.hidden=!(short>0&&cash+used>0&&s.length);
  ps.textContent=_t("Es fehlen {v}, der Rest bleibt offen.").replace('{v}',eur(Math.max(0,short)))+(used?' '+_t("Davon aus Guthaben: {v}").replace('{v}',eur(used)):'');
  if(used&&short<=0){ps.hidden=false;ps.textContent=_t("Davon aus Guthaben: {v}").replace('{v}',eur(used))}
  const vis=boxes().filter(c=>!c.closest('label').hidden);
  f.querySelector('.tog').textContent=vis.length&&vis.every(c=>c.checked)?_t("Keine"):_t("Alle");
  const go=f.querySelector('.go');go.disabled=!s.length||cash+used<=0;
  go.textContent=cash>0?_t("{v} verbuchen").replace('{v}',eur(cash)):used?_t("Aus Guthaben verbuchen"):_t("Verbuchen")}
function err(m){const b=f.querySelector('.bpe');b.textContent=m||'';b.hidden=!m}
function show(body){f.querySelector('.bpbody').hidden=!body;f.querySelector('.bpok').hidden=body}
async function open(q){err('');show(true);typed=false;mode='oldest';f.reset();
  f.querySelectorAll('.bpseg button').forEach(b=>b.classList.toggle('on',b.dataset.m===mode));
  const d=new Date();f.date.value=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  L.textContent='';f.querySelector('.ttl').textContent=_t("Zahlung erfassen");f.querySelector('.sub').textContent='';bg.hidden=false;
  let r;try{r=await(await fetch('api/bulkpay?'+q)).json()}catch(_){r={error:_t("Laden fehlgeschlagen.")}}
  if(r.error){err(r.error);f.querySelector('.bpbody').hidden=true;f.querySelector('.bpok').hidden=false;f.querySelector('.bpok p').textContent='';f.querySelector('.undo').hidden=true;return}
  D=r;f.querySelector('.ttl').textContent=_t("💶 Zahlung von {name}").replace('{name}',D.customer.name);
  D.items.forEach(x=>{const lb=document.createElement('label');const cb=document.createElement('input');cb.type='checkbox';cb.checked=x.on;cb.value=x.id;
    const nm=document.createElement('span');nm.className='nm';const b=document.createElement('b');b.textContent=x.name;
    if(x.code){const p=document.createElement('span');p.className='pid';p.textContent=x.code;b.appendChild(p)}
    const sm=document.createElement('small');sm.textContent=x.sub;nm.append(b,sm);
    const op=document.createElement('span');op.className='op';op.textContent=eur(x.open);const gv=document.createElement('span');gv.className='gv';
    lb.append(cb,nm,op,gv);lb.hidden=!x.on;L.appendChild(lb)});
  const rest=D.items.filter(x=>!x.on).length,mo=f.querySelector('.more');mo.hidden=!rest;
  mo.textContent=(rest==1?_t("+ 1 weiterer offener Posten"):_t("+ {n} weitere offene Posten").replace('{n}',rest));
  if(!D.items.length)err(_t("Nichts offen."));upd();if(!r.items.some(x=>!x.on))f.amount.focus()}
// Eintippen (auch Leeren) übernimmt den Betrag; erst eine neue Postenauswahl füllt ein leeres Feld wieder vor.
const pick=()=>{if(!f.amount.value.trim())typed=false;upd()};
f.amount.addEventListener('input',()=>{typed=true;upd()});
L.addEventListener('change',pick);f.addEventListener('change',ev=>{if(ev.target.name!=='amount')upd()});
f.querySelectorAll('.bpseg button').forEach(b=>b.onclick=()=>{mode=b.dataset.m;f.querySelectorAll('.bpseg button').forEach(x=>x.classList.toggle('on',x===b));upd()});
f.querySelector('.tog').onclick=()=>{const on=!boxes().filter(c=>!c.closest('label').hidden).every(c=>c.checked);
  boxes().forEach(c=>{if(!c.closest('label').hidden)c.checked=on});pick()};
f.querySelector('.more').onclick=ev=>{L.querySelectorAll('label[hidden]').forEach(l=>l.hidden=false);ev.target.hidden=true;pick()};
const close=()=>{bg.hidden=true;if(last)location.reload()};
f.querySelector('.cancel').onclick=close;f.querySelector('.close').onclick=close;bgClose(bg,close);
f.onsubmit=async ev=>{ev.preventDefault();if(!D)return;const fd=new FormData(f);fd.append('customer',D.customer.id);fd.append('mode',mode);
  fd.append('ids',sel().map(x=>x.id).join(','));if(!(D.credit>0))fd.delete('use_credit');
  const go=f.querySelector('.go');go.disabled=true;let r;
  try{r=await(await fetch('api/bulkpay',{method:'POST',body:fd})).json()}catch(_){r={error:_t("Speichern fehlgeschlagen.")}}
  if(r.error){err(r.error);go.disabled=false;return}
  last=r;err('');show(false);f.querySelector('.undo').hidden=false;
  let m=(r.n==1?_t("1 Zahlung verbucht"):_t("{n} Zahlungen verbucht").replace('{n}',r.n))+', '+(r.full==1?_t("1 Posten jetzt bezahlt."):_t("{n} Posten jetzt bezahlt.").replace('{n}',r.full));
  if(r.credit)m+=' '+_t("{v} als Guthaben gemerkt.").replace('{v}',eur(r.credit));
  if(r.used)m+=' '+_t("{v} aus dem Guthaben verwendet.").replace('{v}',eur(r.used));
  f.querySelector('.bpok p').textContent='✓ '+m};
f.querySelector('.undo').onclick=async()=>{if(!last)return;const fd=new FormData();fd.append('payments',last.payments.join(','));fd.append('credits',last.credits.join(','));
  await fetch('api/bulkpay/undo',{method:'POST',body:fd});last={};close()};
document.addEventListener('click',ev=>{const b=ev.target.closest('[data-bulkpay]');if(!b)return;ev.preventDefault();ev.stopPropagation();open('customer='+b.dataset.bulkpay)},true);
window.BP={open}})();
"""


def credit_tag(store: Store, customer_id: int) -> str:
    bal = store.credit_balance(customer_id)
    if abs(bal) < 0.005:
        return ""
    return f'<span class="otg ok calc" title="{e(t("Guthaben aus zu viel Gezahltem"))}">💳 {t("{v} Guthaben", v=fmt_num(bal, 2, "€"))}</span>'
