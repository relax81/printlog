// Screenshots für die README aus dem Demo-Modus.
// Aufruf (App läuft lokal auf Port 8095, sonst PORT=…): node docs/screenshots/capture.js <en|de> <ausgabeordner> <druck-id> [light|dark] [fehldruck-id] [projekt-id]
// Danach: python3 docs/screenshots/shrink.py <ausgabeordner> docs/screenshots/<en|de>[-dark]
// PDF vorher in ein Bild umwandeln: pdftoppm -r 90 -png -singlefile <ausgabeordner>/raw-kosten.pdf <ausgabeordner>/raw-cost
// Druck-ID: ein fertiger Druck mit Kunde, Fotos und mehreren Filamenten (die Demo-Daten sind fest vorgegeben, z. B. 26).
// Fehldruck-ID: der anteilig berechnete Fehldruck der Demo (Status-Filter „Fehldruck: Einstellungen“, z. B. 226).
// Projekt-ID: das Beispielprojekt „Werkstatt-Wand“ der Demo (z. B. 360).
// Zubehör-ID: ein Schlüsselanhänger-Druck mit Ring, Kette und Arbeitszeit (z. B. 88).
const prevMonth = () => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); };
const { chromium, devices } = require('playwright');
const [lang, out, job, scheme = 'light', fd, proj, zk] = process.argv.slice(2);
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
  {
    // Kunde zuordnen: Chip mit Vorschlag antippen, Schnellauswahl offen
    await p.setViewportSize({ width: 1440, height: 900 });
    await p.evaluate(() => sessionStorage.removeItem('pl_open'));
    await p.goto(base); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const chip = await p.$('.jr .kchip.sug .kc');
    await p.evaluate(el => scrollTo(0, el.closest('.jr').getBoundingClientRect().top + scrollY - 130), chip);
    await chip.click(); await p.waitForTimeout(300);
    const r = await p.evaluate(() => { const pop = document.getElementById('kpop').getBoundingClientRect();
      const rows = [...document.querySelectorAll('.jr')].filter(el => { const b = el.getBoundingClientRect(); return b.bottom > pop.top - 60 && b.top < pop.bottom; });
      const top = Math.min(pop.top, rows[0].getBoundingClientRect().top), bottom = Math.max(pop.bottom, rows[rows.length - 1].getBoundingClientRect().bottom);
      return { y: top + scrollY, h: bottom - top }; });
    await p.screenshot({ path: `${out}/assign-customer.png`, clip: { x: 0, y: r.y - 8, width: 1440, height: r.h + 16 }, fullPage: true });
  }
  {
    // Aufteilen: Zeile in der Liste, Kasten „Aufteilung“, Fenster „Aufteilen“, Ansicht „Vorrat“ (aufgeteilte Drucke der Demo)
    await p.setViewportSize({ width: 1440, height: 900 });
    await p.goto(base + '?view=stock'); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const href = await p.$eval('.vr .oname a', a => a.getAttribute('href').split('#')[0]);
    await p.screenshot({ path: `${out}/stock.png` });
    await p.goto(base); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const row = await p.evaluate(h => { const el = document.querySelector(`.jr[data-href="${h}"]`); const b = el.getBoundingClientRect();
      return { y: b.top + scrollY, h: b.height }; }, href);
    await p.screenshot({ path: `${out}/split-list.png`, clip: { x: 0, y: row.y - 8, width: 1440, height: row.h + 16 }, fullPage: true });
    await p.goto(base + href); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const y = await p.$eval('.spbox', el => el.getBoundingClientRect().bottom + scrollY);
    await p.screenshot({ path: `${out}/split-detail.png`, clip: { x: 0, y: 0, width: 1440, height: y + 16 }, fullPage: true });
    await p.click('.spbox .spopen'); await p.waitForTimeout(400);
    const d = await p.$eval('.spdlg', el => { const b = el.getBoundingClientRect(); return { y: b.top, h: b.height }; });
    await p.screenshot({ path: `${out}/split-dialog.png`, clip: { x: 360, y: Math.max(0, d.y - 40), width: 720, height: d.h + 80 } });
  }
  if (zk) {
    // Arbeit & Zubehör: Kasten in der Übersicht, Inventar-Auswahl, Inventar in den Einstellungen
    await p.setViewportSize({ width: 1440, height: 900 });
    await p.goto(base + `job?id=${zk}`); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const r = await p.$eval('.zk', el => { const b = el.getBoundingClientRect(); return { y: b.top + scrollY, h: b.height }; });
    await p.screenshot({ path: `${out}/work-accessories.png`, clip: { x: 0, y: r.y - 12, width: 1440, height: r.h + 24 }, fullPage: true });
    await p.click('.zk [data-zk-pick]'); await p.waitForTimeout(400);
    const d = await p.$eval('#zkpick .d', el => { const b = el.getBoundingClientRect(); return { x: b.left, y: b.top, w: b.width, h: b.height }; });
    await p.screenshot({ path: `${out}/accessory-picker.png`, clip: { x: d.x - 30, y: Math.max(0, d.y - 30), width: d.w + 60, height: d.h + 60 } });
    await p.setViewportSize({ width: 1440, height: 1300 });
    await p.goto(base + 'storage'); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
    const s = await p.$eval('table.acc', el => { const b = el.getBoundingClientRect(); return { y: b.top + scrollY, h: b.height }; });
    await p.screenshot({ path: `${out}/accessory-inventory.png`, clip: { x: 0, y: 0, width: 1440, height: s.y + s.h + 16 }, fullPage: true });
  }
  // Lager: Lagerorte und Seite eines Lagerorts (der mit Drucken aus dem Vorrat)
  await shot('locations', 'storage?tab=orte', { h: 620 });
  const loc = await p.$eval('.locc:has(img)', a => a.getAttribute('href'));
  await shot('location', loc, { h: 700 });
  // Modelle: Hinweise (Glocke) und Modellliste
  await shot('hints', 'hints', { h: 760 });
  await shot('models', 'models', { h: 860 });
  // Aufträge: Übersicht und ein Auftrag in Arbeit
  await shot('orders', 'orders', { h: 820 });
  const ord = await p.evaluate(() => (document.querySelector('.ocard.running') || document.querySelector('.ocard')).getAttribute('href'));
  await shot('order-detail', ord, { h: 980 });
  // phone
  const ph = await b.newContext({ ...devices['iPhone 13'], locale: lang, colorScheme: scheme, extraHTTPHeaders: hdr });
  await ph.addInitScript(init);
  const q = await ph.newPage();
  for (const [n, u] of [['phone-list', ''], ['phone-detail', `job?id=${job}`], ['phone-open', '?view=todo'], ['phone-stock', '?view=stock']]) {
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
