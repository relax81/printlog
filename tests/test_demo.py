"""Demo-Modus: Beispieldaten landen nur in der Demo-Datenbank und decken alle Ansichten ab.

Aufruf: python -m pytest tests
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.demo import seed  # noqa: E402


def test_demo_daten() -> None:
    root = Path(tempfile.mkdtemp())
    echt = Store(root / "printlog.db")
    echt.set_setting("language", "de")
    demo = Store(root / "demo" / "printlog.db")
    images = root / "demo" / "images"
    now = time.time()
    n = seed(demo, images, "de", now)

    assert n > 200
    assert echt.count_jobs() == 0 and not echt.customers()  # echte Daten unberührt
    assert {p["printer"] for p in demo.printers()} == {"a1mini", "p1s", "h2s"}
    assert demo.counts()["running"] == 2
    assert demo.trash_count() == 2
    assert demo.count_jobs(status="ausschuss") > 0
    assert demo.count_jobs(status="unvollständig") > 0
    assert demo.count_jobs(paid="verschenkt") > 0
    assert demo.count_jobs(paid="teilweise") > 0
    assert demo.count_jobs(pending="1") > 0
    assert any(c["no_billing"] for c in demo.customers())
    first = demo.counts()["first_ts"]
    assert now - first > 300 * 86400  # ein Jahr Historie für die Statistik
    jobs = demo.list_jobs(limit=1000)
    assert all(j["image"] and (images / j["image"]).is_file() for j in jobs)
    assert any(demo.photos(j["id"]) for j in jobs)
    assert all(j["cost_total"] for j in jobs if j["state"] == "done" and j["status"] == "fertig")

    # Neu erzeugen ersetzt alles, die Sprache bleibt
    demo.set_setting("language", "en")
    seed(demo, images, "en", now)
    assert demo.settings()["language"] == "en"
    assert demo.list_jobs(limit=1)[0]["task_name"] in {"Gridfinity bin 2x3", "Voronoi lamp shade"}
    assert len(list(images.iterdir())) == len(demo.image_files())
