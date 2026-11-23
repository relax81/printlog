# Icons und Logo

- **App-Icon:** Entwurf A „Bon“ (Nozzle druckt einen Kassenbon mit €). Quelle `app/static/icon.svg`, maskierbare Variante `icon-maskable.svg` (für Android-Adaptive-Icons und Apple-Touch-Icon, Motiv in der sicheren Zone).
- **Schriftzug:** Entwurf D „printl€g“ als einzeilige Wortmarke, `app/static/logo.svg`. Text ist in Pfade umgewandelt (Inter Display Black) und nutzt `currentColor`, folgt also dem Hell-/Dunkel-Theme.
- **Entwürfe:** Alle sechs Vorschläge aus der Auswahl liegen in `entwuerfe/` (SVG + PNG, `uebersicht.png` zeigt sie in 256/96/48 px).

Neu erzeugen:

```sh
pip install fonttools
python assets/icons/build.py   # SVGs nach app/static/
node assets/icons/render.js    # PNG-Größen (Playwright)
```

Die App liefert alles unter `/static/` aus, das Manifest unter `/manifest.webmanifest`.
