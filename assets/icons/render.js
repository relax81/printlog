// Rendert die PNG-Größen aus den SVGs (Playwright/Chromium). Aufruf: node assets/icons/render.js
const {chromium} = require('playwright'); const fs = require('fs'); const path = require('path');
const root = path.resolve(__dirname, '..', '..'); const st = path.join(root, 'app', 'static');
const jobs = [
  ['app/static/icon.svg', 'app/static/icon-512.png', 512],
  ['app/static/icon.svg', 'app/static/icon-192.png', 192],
  ['app/static/icon.svg', 'app/static/favicon-32.png', 32],
  ['assets/icons/icon-maskable.svg', 'app/static/icon-maskable-512.png', 512],
  ['assets/icons/icon-maskable.svg', 'app/static/apple-touch-icon.png', 180],
];
(async () => {
  const b = await chromium.launch();
  for (const [src, out, s] of jobs) {
    const p = await b.newPage({viewport: {width: s, height: s}});
    const svg = fs.readFileSync(path.join(root, src), 'utf8').replace(/width="512" height="512"/, `width="${s}" height="${s}"`);
    await p.setContent(`<style>html,body{margin:0;background:transparent}svg{display:block}</style>${svg}`);
    await p.screenshot({path: path.join(root, out), omitBackground: true}); await p.close();
  }
  // Schriftzug in Dunkel für den Bon-Export (app/export.py), 140 px hoch
  const logo = fs.readFileSync(path.join(st, 'logo.svg'), 'utf8').replace('<svg ', '<svg height="140" ');
  const lp = await b.newPage({viewport: {width: 600, height: 140}});
  await lp.setContent(`<style>html,body{margin:0;background:transparent;color:#24211d}svg{display:block}</style>${logo}`);
  await (await lp.$('svg')).screenshot({path: path.join(st, 'logo-bon.png'), omitBackground: true}); await lp.close();
  await b.close();
})();
