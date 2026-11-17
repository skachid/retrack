"""Command line entry point.

    python -m retrack catalog.csv --mock
    python -m retrack catalog.csv --out report.csv
    python -m retrack catalog.csv --smart-names   # needs ANTHROPIC_API_KEY
    python -m retrack catalog.csv --ai-search     # needs ANTHROPIC_API_KEY
    python -m retrack catalog.csv --mock --ai-search  # offline version, no key
    python -m retrack catalog.csv --mock --html report.html
    python -m retrack catalog.csv --mock --serve  # opens http://localhost:8000
"""

import argparse
import sys
from pathlib import Path

from .checker import check_catalog, load_catalog
from .mlc_client import MLCClient, MLCError, MockClient
from .name_matcher import ClaudeNameMatcher, MockNameMatcher
from .work_search import ClaudeWorkSearcher, MockWorkSearcher
from .preview import serve
from .report import print_summary, render_html, write_csv


def main(argv=None):
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
        if args.serve:
            return serve(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
