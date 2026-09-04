"""Per-bank UPI health (spec 6.1).

Tiering, in order of preference:
  1. live fetch from NPCI's monthly BD/TD & uptime publication;
  2. a hand transcription of one recent NPCI month;
  3. clearly-labelled SYNTHETIC data.

This build ships at TIER 3. The NPCI page is JS-rendered and no value here was
transcribed from NPCI, so nothing may be presented as NPCI data. `provenance`
propagates into the evidence bundle and into the report banner.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from statistics import median

from grace.util import ensure_aware

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SYNTHETIC = DATA_DIR / "bank_health_SYNTHETIC.csv"
SNAPSHOT = DATA_DIR / "bank_health_snapshot.csv"
DOWNTIME = DATA_DIR / "downtime_events.json"

NPCI_URL = "https://www.npci.org.in/statistics/bd-td-and-uptime"


def try_fetch_npci_latest(timeout: float = 20.0) -> Path | None:
    """Attempt tier 1. Returns the written snapshot path, or None.

    Deliberately best-effort: the NPCI page renders its file list with
    JavaScript, so a plain HTTP fetch usually finds no XLSX link. Never raises.
    """
    try:
        import re

        import httpx

        html = httpx.get(NPCI_URL, timeout=timeout, follow_redirects=True).text
        links = re.findall(r'href="([^"]+\.(?:xlsx|xls))"', html, flags=re.I)
        if not links:
            return None
        # A real implementation would parse the workbook here with openpyxl and
        # write SNAPSHOT with a provenance header. Left unimplemented rather
        # than guessed: writing an unverified parser would produce numbers that
        # look like NPCI data but are not.
        return None
    except Exception:
        return None


class BankHealth:
    def __init__(self, path: Path | str | None = None):
        if path is not None:
            self.path = Path(path)
        elif SNAPSHOT.exists():
            self.path = SNAPSHOT
        else:
            self.path = SYNTHETIC
        self.rows: dict[str, dict] = {}
        self.provenance = "unknown"
        self.is_synthetic = True
        self.month = "unknown"
        self._load()
        self._windows = self._load_downtime()

    def _load(self) -> None:
        if not self.path.exists():
            self.provenance = "MISSING"
            return
        header_lines, data_lines = [], []
        for line in self.path.read_text().splitlines():
            (header_lines if line.startswith("#") else data_lines).append(line)
        for h in header_lines:
            if "source=" in h:
                self.provenance = h.lstrip("# ").strip()
                break
        self.is_synthetic = "SYNTHETIC" in self.provenance.upper()
        for row in csv.DictReader(data_lines):
            self.rows[row["bank"].strip()] = {
                "td_pct": float(row["td_pct"]),
                "bd_pct": float(row["bd_pct"]),
                "uptime_pct": float(row["uptime_pct"]),
                "month": row["month"],
            }
        if self.rows:
            self.month = next(iter(self.rows.values()))["month"]
        self._median = {
            k: median([r[k] for r in self.rows.values()]) if self.rows else 0.0
            for k in ("td_pct", "bd_pct", "uptime_pct")
        }

    def _load_downtime(self) -> list[dict]:
        if not DOWNTIME.exists():
            return []
        raw = json.loads(DOWNTIME.read_text())
        out = []
        for w in raw.get("windows", []):
            out.append({
                "bank": w["bank"],
                "start": ensure_aware(datetime.fromisoformat(w["start"])),
                "end": ensure_aware(datetime.fromisoformat(w["end"])),
                "note": w.get("note", ""),
            })
        return out

    def banks(self) -> list[str]:
        return sorted(self.rows)

    def get(self, bank: str) -> dict:
        """Bank health with provenance. Unknown bank falls back to the cohort median."""
        if bank in self.rows:
            r = self.rows[bank]
            return {**r, "provenance": self.provenance, "synthetic": self.is_synthetic}
        return {
            **self._median,
            "month": self.month,
            "provenance": f"{self.provenance} (bank not listed; cohort median)",
            "synthetic": self.is_synthetic,
        }

    def is_in_downtime(self, bank: str, ts: datetime) -> bool:
        ts = ensure_aware(ts)
        return any(w["bank"] == bank and w["start"] <= ts <= w["end"] for w in self._windows)

    def downtime_note(self, bank: str, ts: datetime) -> str | None:
        ts = ensure_aware(ts)
        for w in self._windows:
            if w["bank"] == bank and w["start"] <= ts <= w["end"]:
                return w["note"]
        return None
