// Screenshots für die README aus dem Demo-Modus.
// Aufruf (App läuft lokal auf Port 8095, sonst PORT=…): node docs/screenshots/capture.js <en|de> <ausgabeordner> <druck-id> [light|dark] [fehldruck-id] [projekt-id]
// Danach: python3 docs/screenshots/shrink.py <ausgabeordner> docs/screenshots/<en|de>[-dark]
// PDF vorher in ein Bild umwandeln: pdftoppm -r 90 -png -singlefile <ausgabeordner>/raw-kosten.pdf <ausgabeordner>/raw-cost
// Druck-ID: ein fertiger Druck mit Kunde, Fotos und mehreren Filamenten (die Demo-Daten sind fest vorgegeben, z. B. 26).
// Fehldruck-ID: der anteilig berechnete Fehldruck der Demo (Status-Filter „Fehldruck: Einstellungen“, z. B. 226).
// Projekt-ID: das Beispielprojekt „Werkstatt-Wand“ der Demo (die höchste ID, z. B. 359).
const prevMonth = () => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); };
const { chromium, devices } = require('playwright');
const [lang, out, job, scheme = 'light', fd, proj] = process.argv.slice(2);
const base = `http://127.0.0.1:${process.env.PORT || 8095}/demo/`;
const hdr = { 'Accept-Language': lang };
(async () => {
  const b = await chromium.launch();
  const init = () => { try { sessionStorage.setItem('pl_demobar', '0') } catch (_) {} };
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, locale: lang, colorScheme: scheme, extraHTTPHeaders: hdr });
  await ctx.addInitScript(init);
  const p = await ctx.newPage();
  await p.goto(base); await p.waitForTimeout(500); // trigger seed in this language
  const shot = async (name, url, opt = {}) => {
    await p.setViewportSize({ width: opt.w || 1440, height: opt.h || 900 });
    await p.goto(base + url); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    if (opt.before) await opt.before();
    await p.screenshot({ path: `${out}/${name}.png`, fullPage: !!opt.full, clip: opt.clip });
  };
  await shot('prints', '');
  await shot('prints-filtered', '?printer=p1s&material=PETG');
  await shot('gallery', '?view=gallery');
  await shot('open-items', '?view=todo');
  await shot('print-detail', `job?id=${job}`);
  await shot('print-billing', `job?id=${job}#ab`);
  await shot('statistics', 'stats', { h: 1130 });
  await shot('calendar', `calendar?month=${prevMonth()}`, { h: 950 });
  await shot('customers', 'customers', { h: 900 });
  if (fd) {
    // Fehldruck: Kopf + Fehldruck-Kasten; Statistik: die beiden Fehldruck-Karten
    const box = async (sel, pad) => { const r = await p.$eval(sel, el => { const b = el.getBoundingClientRect(); return { y: b.bottom + scrollY }; }); return r.y + pad; };
    await p.setViewportSize({ width: 1440, height: 900 });
    await p.goto(base + `job?id=${fd}`); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    await p.screenshot({ path: `${out}/failed-print.png`, clip: { x: 0, y: 0, width: 1440, height: await box('.fdbox', 16) }, fullPage: true });
    await p.goto(base + 'stats'); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const g = await p.$eval('.fdcard', el => { const b = el.parentElement.getBoundingClientRect(); return { y: b.top + scrollY, h: b.height }; });
    await p.screenshot({ path: `${out}/failed-print-stats.png`, clip: { x: 0, y: g.y - 12, width: 1440, height: g.h + 24 }, fullPage: true });
  }
  if (proj) {
    // Projekt: aufgeklappte Zeile in der Liste und die Projektseite
    await p.setViewportSize({ width: 1440, height: 900 });
    await p.goto(base); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    await p.click(`.xp[data-pid="${proj}"]`); await p.waitForTimeout(300);
    const r = await p.evaluate(id => {
      const rows = [...document.querySelectorAll(`.jr[data-href="job?id=${id}"], .jr[data-parent="${id}"]`)].filter(el => el.offsetParent);
      const top = rows[0].getBoundingClientRect().top + scrollY, last = rows[rows.length - 1].getBoundingClientRect();
      return { y: top, h: last.bottom + scrollY - top };
    }, proj);
    await p.screenshot({ path: `${out}/project-list.png`, clip: { x: 0, y: r.y - 8, width: 1440, height: r.h + 16 }, fullPage: true });
    await shot('project-detail', `job?id=${proj}`, { full: true });
  }
  // phone
  const ph = await b.newContext({ ...devices['iPhone 13'], locale: lang, colorScheme: scheme, extraHTTPHeaders: hdr });
  await ph.addInitScript(init);
  const q = await ph.newPage();
  for (const [n, u] of [['phone-list', ''], ['phone-detail', `job?id=${job}`], ['phone-open', '?view=todo']]) {
    await q.goto(base + u); await q.waitForLoadState('networkidle'); await q.waitForTimeout(600);
    await q.screenshot({ path: `${out}/${n}.png` });
  }
  // receipts
  for (const k of ['bon-kunde.png', 'bon-intern.png', 'kosten.pdf']) {
    const r = await ctx.request.get(base + `export/${job}/${k}`, { headers: hdr });
    require('fs').writeFileSync(`${out}/raw-${k}`, await r.body());
  }
  await b.close();
})();
