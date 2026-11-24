// Screenshots für die README aus dem Demo-Modus.
// Aufruf (App läuft lokal auf Port 8095): node docs/screenshots/capture.js <en|de> <ausgabeordner> <druck-id>
// Danach: python3 docs/screenshots/shrink.py <ausgabeordner> docs/screenshots/<en|de>
// PDF vorher in ein Bild umwandeln: pdftoppm -r 90 -png -singlefile <ausgabeordner>/raw-kosten.pdf <ausgabeordner>/raw-cost
// Druck-ID: ein fertiger Druck mit Kunde, Fotos und mehreren Filamenten (die Demo-Daten sind fest vorgegeben, z. B. 26).
const prevMonth = () => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); };
const { chromium, devices } = require('playwright');
const [lang, out, job] = process.argv.slice(2);
const base = 'http://127.0.0.1:8095/demo/';
const hdr = { 'Accept-Language': lang };
(async () => {
  const b = await chromium.launch();
  const init = () => { try { sessionStorage.setItem('pl_demobar', '0') } catch (_) {} };
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, locale: lang, extraHTTPHeaders: hdr });
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
  // phone
  const ph = await b.newContext({ ...devices['iPhone 13'], locale: lang, extraHTTPHeaders: hdr });
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
