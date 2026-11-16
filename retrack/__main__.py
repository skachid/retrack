"""Command line entry point.

    python -m retrack catalog.csv --mock
    python -m retrack catalog.csv --out report.csv
    python -m retrack catalog.csv --smart-names   # needs ANTHROPIC_API_KEY
"""

import argparse
import sys

from .checker import check_catalog, load_catalog
from .mlc_client import MLCClient, MLCError, MockClient
from .name_matcher import ClaudeNameMatcher, MockNameMatcher
from .report import print_summary, write_csv


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check a music catalog against The MLC for royalty gaps.")
    parser.add_argument("catalog", help="CSV with columns: title, artist, isrc, writers (writers split by ';')")
    parser.add_argument("--out", default="report.csv", help="where to save the CSV report (default: report.csv)")
    parser.add_argument("--mock", action="store_true", help="use fake MLC data from fixtures/ instead of the real API")
    parser.add_argument("--smart-names", action="store_true",
                        help="when a writer name doesn't match exactly, ask the Claude API if it's "
                             "the same person (sends only writer names; needs ANTHROPIC_API_KEY)")
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
        results = check_catalog(rows, client, name_matcher)
    except ImportError:
        print("Error: --smart-names needs the anthropic package: pip install anthropic", file=sys.stderr)
        return 1
    except (MLCError, ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print_summary(results)
    write_csv(results, args.out)
    print(f"\nFull report saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
