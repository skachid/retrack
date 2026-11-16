"""Turns results into something a human can read: terminal summary + CSV."""

import csv

from .checker import GREEN, RED, YELLOW

ICONS = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴"}
ORDER = {RED: 0, YELLOW: 1, GREEN: 2}


def print_summary(results):
    counts = {s: sum(1 for r in results if r.status == s) for s in (RED, YELLOW, GREEN)}
    print(f"\nChecked {len(results)} songs: "
          f"{counts[RED]} red, {counts[YELLOW]} yellow, {counts[GREEN]} green\n")
    for r in sorted(results, key=lambda r: ORDER[r.status]):
        print(f"{ICONS[r.status]} {r.row.title} — {r.row.artist} ({r.row.isrc})")
        for p in r.problems:
            print(f"     ✗ {p}")
        for fix in r.fixes:
            print(f"     → {fix}")
        for note in r.notes:
            print(f"     ≈ {note}")
    print("\nAlso check by hand (no public API for these):")
    print("  • SoundExchange ISRC search: https://isrc.soundexchange.com/")
    print("  • ASCAP/BMI Songview:        https://repertoire.bmi.com/")


def write_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "title", "artist", "isrc", "mlc_song_code", "iswc",
                    "claimed_share_pct", "mlc_writers", "problems", "fixes", "notes"])
        for r in sorted(results, key=lambda r: ORDER[r.status]):
            w.writerow([r.status, r.row.title, r.row.artist, r.row.isrc, r.song_code, r.iswc,
                        r.claimed_share, "; ".join(r.mlc_writers),
                        " | ".join(r.problems), " | ".join(r.fixes), " | ".join(r.notes)])
