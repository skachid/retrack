"""Command line entry point.

    python -m retrack import my_sheet.xlsx --out catalog.csv   # any spreadsheet -> catalog.csv
    python -m retrack catalog.csv --mock
    python -m retrack catalog.csv --out report.csv
    python -m retrack catalog.csv --smart-names   # needs ANTHROPIC_API_KEY
    python -m retrack catalog.csv --ai-search     # needs ANTHROPIC_API_KEY
    python -m retrack catalog.csv --mock --ai-search  # offline version, no key
    python -m retrack catalog.csv --mock --html report.html
    python -m retrack catalog.csv --mock --serve  # opens http://localhost:8000
    python -m retrack catalog.csv --mock --ai-search --fix            # fix pack for every fixable song
    python -m retrack catalog.csv --ai-search --fix-songs "Blue Hour" --template mlc_template.xlsx
"""

import argparse
import sys
from pathlib import Path

from . import importer
from .checker import check_catalog, load_catalog
from .fixes import (FIXES_DIR, TEMPLATE_HELP, FixPackError, ask_for_attestation, build_fix_pack,
                    check_template, confirm, plan_fixes, price_line)
from .mlc_client import MLCClient, MLCError, MockClient
from .name_matcher import ClaudeNameMatcher, MockNameMatcher
from .work_search import ClaudeWorkSearcher, MockWorkSearcher
from .preview import serve
from .report import print_summary, render_html, write_csv


def main(argv=None, input_fn=input, stdin_is_tty=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["import"]:  # python -m retrack import my_sheet.xlsx --out catalog.csv
        return importer.main(argv[1:], input_fn=input_fn, stdin_is_tty=stdin_is_tty)

    parser = argparse.ArgumentParser(description="Check a music catalog against The MLC for royalty gaps.")
    parser.add_argument("catalog", help="CSV with columns: title, artist, isrc, writers (writers split by ';')")
    parser.add_argument("--out", default="report.csv", help="where to save the CSV report (default: report.csv)")
    parser.add_argument("--mock", action="store_true", help="use fake MLC data from fixtures/ instead of the real API")
    parser.add_argument("--smart-names", action="store_true",
                        help="when a writer name doesn't match exactly, ask the Claude API if it's "
                             "the same person (sends only writer names; needs ANTHROPIC_API_KEY)")
    parser.add_argument("--ai-search", action="store_true",
                        help="for red songs and songs with missing writers, search The MLC by title "
                             "and writer names (with variants suggested by the Claude API) to find "
                             "registrations that aren't linked to the ISRC. Sends only titles and "
                             "writer names; needs ANTHROPIC_API_KEY. With --mock it runs offline.")
    parser.add_argument("--html", metavar="PATH",
                        help="also save a readable, printable HTML report (e.g. report.html)")
    parser.add_argument("--serve", action="store_true",
                        help="show the HTML report at http://localhost:8000 and open your browser "
                             "(this computer only; Ctrl+C to stop)")
    fix = parser.add_mutually_exclusive_group()
    fix.add_argument("--fix", action="store_true",
                     help=f"make a fix pack in {FIXES_DIR}/ for every song that can be fixed")
    fix.add_argument("--fix-songs", metavar='"TITLE A,TITLE B"',
                     help="make a fix pack for these songs only (titles, comma-separated)")
    parser.add_argument("--template", metavar="PATH.xlsx",
                        help="the Bulk Work Registration template you downloaded from your own MLC "
                             "Portal. Retrack fills a copy of it; the original isn't changed.")
    parser.add_argument("--i-confirm", action="store_true",
                        help="for scripts: confirm you reviewed the data and control the shares "
                             "(instead of answering the prompt)")
    args = parser.parse_args(argv)

    try:
        rows = load_catalog(args.catalog)
        client = MockClient() if args.mock else MLCClient()
        if args.mock:
            name_matcher = MockNameMatcher()  # offline rules, no API key needed
        elif args.smart_names:
            name_matcher = ClaudeNameMatcher()
        else:
            name_matcher = None  # basic matching only
        if not args.ai_search:
            work_searcher = None  # opt-in only: it costs API calls
        elif args.mock:
            work_searcher = MockWorkSearcher()  # offline rules, no API key needed
        else:
            work_searcher = ClaudeWorkSearcher()
        results = check_catalog(rows, client, name_matcher, work_searcher)
    except ImportError:
        print("Error: --smart-names and --ai-search need the anthropic package: pip install anthropic",
              file=sys.stderr)
        return 1
    except (MLCError, ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print_summary(results)
    write_csv(results, args.out)
    print(f"\nFull report saved to {args.out}")

    if args.html or args.serve:
        html = render_html(results, catalog_name=Path(args.catalog).name, mock=args.mock)
        if args.html:
            Path(args.html).write_text(html, encoding="utf-8")
            print(f"HTML report saved to {args.html}")
        if args.serve and not (args.fix or args.fix_songs):
            return serve(html)

    if args.fix or args.fix_songs:
        if stdin_is_tty is None:
            stdin_is_tty = sys.stdin.isatty()
        return make_fix_pack(results, args, input_fn, stdin_is_tty)
    return 0


def make_fix_pack(results, args, input_fn, stdin_is_tty):
    """Plan -> check the template -> confirm -> write. Nothing is written until the user confirms."""
    titles = args.fix_songs.split(",") if args.fix_songs else None
    try:
        plan = plan_fixes(results, titles)
        if args.template:
            check_template(args.template)
    except FixPackError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print("\nFix pack")
    for label, songs in (("Register (not found anywhere)", plan.register),
                         ("Link (registered but not linked)", plan.link),
                         ("Claim (shares or writers)", plan.claim)):
        for r in songs:
            print(f"  {label + ':':<34} {r.row.title}")
    for r, why in plan.skipped:
        print(f"  {'Skipped:':<34} {r.row.title} ({why})")
    if not plan.songs:
        print("  Nothing to fix.")
        return 0
    print(f"\n  {price_line(plan.songs)}")
    if plan.register and not args.template:
        print(f"\n  No --template given, so registrations go in a CSV worksheet instead. {TEMPLATE_HELP}")

    if args.i_confirm:
        attestation = confirm("--i-confirm flag")
    elif stdin_is_tty:
        attestation = ask_for_attestation(input_fn)
    else:
        attestation = None
    if attestation is None:
        print("\nNo confirmation, so no files were created. Answer the prompt with yes, "
              "or pass --i-confirm.", file=sys.stderr)
        return 1

    try:
        written = build_fix_pack(plan, Path(FIXES_DIR), attestation, args.template)
    except FixPackError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"\nFix pack saved to {FIXES_DIR}/ (start with README.md):")
    for name in written:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
