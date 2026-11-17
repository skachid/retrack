"""Rebuilds the .xlsx import fixtures. Run from the project root:

    python fixtures/import/build_import_fixtures.py

All data is made up. The files are shaped like real exports (column names and
layout), not copies of them.
"""

from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent


def tunecore():
    """Shaped like a TuneCore catalog export: one row per song, writers in
    numbered columns, some 'Last, First' names and '/' separators."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalog"
    ws.append(["Release Title", "Release Type", "UPC", "Song Title", "Artist Name", "Optional ISRC",
               "TC Song ID", "Songwriter 1", "Songwriter 2", "Label"])
    ws.append(["Night Shifts", "Album", "859700000011", "Late Night Drive", "Nova Reyes", "QZABC2400001",
               "TC1001", "Nova Reyes", "", "Sunbeam Records"])
    ws.append(["Night Shifts", "Album", "859700000011", "Paper Crowns", "Nova Reyes", "qzabc2400002",
               "TC1002", "Nova Reyes", "Marcus Bell", "Sunbeam Records"])
    ws.append(["Greenhouse", "EP", "859700000028", "Concrete Garden", "The Lowlights", "QZABC2400003",
               "TC2001", "Jide Okafor / Sam Lee", "", "Sunbeam Records"])
    ws.append(["Greenhouse", "EP", "859700000028", "Ghost Signal", "The Lowlights", "QZABC2400004",
               "TC2002", "Lee, Sam", "", "Sunbeam Records"])
    wb.save(HERE / "tunecore_export.xlsx")


def messy_label_sheet():
    """A hand-made label spreadsheet: a title row, a note, blank rows, merged
    artist cells, a header repeated halfway down, typos and duplicates."""
    wb = Workbook()
    notes = wb.active
    notes.title = "Notes"
    notes["A1"] = "Ask Dana before changing anything!"
    ws = wb.create_sheet("Songs 2024")
    ws["A1"] = "SUNBEAM RECORDS: song list"
    ws.merge_cells("A1:F1")
    ws["A2"] = "updated by Dana, March"
    headers = ["#", "Song Name", "Performer", "ISRC code", "Composer(s)", "Notes"]
    rows = [
        headers,                                                                       # row 4
        [1, "Late Night Drive", "Nova Reyes", "qz-abc-24-00001", "Nova Reyes", ""],     # 5
        [2, "Paper Crowns", "Nova Reyes", "QZ ABC 24 00002", "Nova Reyes & Marcus Bell", "single"],  # 6
        [3, "Concrete Garden", None, "QZABC2400003", "Jide Okafor / Sam Lee", ""],     # 7 (artist merged from 6)
        [],                                                                            # 8 blank
        [4, "Ghost Signal", "The Lowlights", "QZABC2400004", "Lee, Sam", ""],          # 9 unsure comma
        [5, "Blue Hour", "The Lowlights", "QZABC240005", "Jide Okafor", "check code"],  # 10 ISRC too short
        [6, "Untitled demo", "The Lowlights", "", "Sam Lee", "no ISRC yet"],           # 11
        [1, "Late Night Drive", "Nova Reyes", "qz-abc-24-00001", "Nova Reyes", ""],     # 12 exact duplicate of 5
        headers,                                                                       # 13 header repeated
        [7, "Static Bloom", "The Lowlights", "QZABC2400006", "", ""],                  # 14 no writers
        [8, "Low Tide", "The Lowlights", "QZABC2400007", "Ana Lopez, Ben Cho and Sam Lee", ""],  # 15
    ]
    for i, row in enumerate(rows, start=4):
        for j, value in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=value)
    ws.merge_cells("C6:C7")  # "Nova Reyes" over Paper Crowns and Concrete Garden
    wb.active = 1
    wb.save(HERE / "label_catalog_messy.xlsx")


if __name__ == "__main__":
    tunecore()
    messy_label_sheet()
    print("Rebuilt fixtures/import/*.xlsx")
