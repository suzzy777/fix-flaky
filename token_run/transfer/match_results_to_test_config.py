#!/usr/bin/env python3
import argparse
import csv
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Match the 'container' column in a results CSV to the "
            "'result_container' issue-id column in test_config.csv."
        )
    )
    parser.add_argument("results_csv", help="CSV containing a 'container' column")
    parser.add_argument(
        "test_config_csv",
        nargs="?",
        default="test_config.csv",
        help="ReproFlake test_config.csv (default: test_config.csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="matched_results_td.csv",
        help="Output CSV (default: matched_results_td.csv)",
    )
    parser.add_argument(
        "--only-matched",
        action="store_true",
        help="Exclude rows whose container is not found in test_config.csv",
    )
    args = parser.parse_args()

    with open(args.test_config_csv, newline="", encoding="utf-8-sig") as f:
        config_reader = csv.DictReader(f)
        if not config_reader.fieldnames:
            raise SystemExit("ERROR: test_config.csv has no header")

        issue_col = None
        for candidate in ("issue_id", "result_container"):
            if candidate in config_reader.fieldnames:
                issue_col = candidate
                break

        if issue_col is None:
            raise SystemExit(
                "ERROR: test_config.csv must contain 'issue_id' "
                "or 'result_container'"
            )

        config_rows = {}
        for row in config_reader:
            issue_id = (row.get(issue_col) or "").strip()
            if issue_id:
                config_rows[issue_id] = row

        config_fields = list(config_reader.fieldnames)

    with open(args.results_csv, newline="", encoding="utf-8-sig") as f:
        results_reader = csv.DictReader(f)
        if not results_reader.fieldnames:
            raise SystemExit("ERROR: results CSV has no header")
        if "container" not in results_reader.fieldnames:
            raise SystemExit("ERROR: results CSV must contain a 'container' column")

        result_fields = list(results_reader.fieldnames)

        # Avoid duplicate column names when combining both CSVs.
        appended_fields = [
            field for field in config_fields
            if field not in result_fields
        ]
        # output_fields = result_fields + ["matched"] + appended_fields

        matched = 0
        unmatched = 0

        # with open(args.output, "w", newline="", encoding="utf-8") as out:
        #     writer = csv.DictWriter(out, fieldnames=output_fields)
        #     writer.writeheader()

        #     for result_row in results_reader:
        #         issue_id = (result_row.get("container") or "").strip()
        #         config_row = config_rows.get(issue_id)

        #         if config_row is None:
        #             unmatched += 1
        #             if args.only_matched:
        #                 continue
        #             merged = dict(result_row)
        #             merged["matched"] = "NO"
        #         else:
        #             matched += 1
        #             merged = dict(result_row)
        #             merged["matched"] = "YES"
        #             for field in appended_fields:
        #                 merged[field] = config_row.get(field, "")

        #         writer.writerow(merged)

        output_fields = config_fields

        with open(args.output, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=output_fields)
            writer.writeheader()

            for result_row in results_reader:
                issue_id = (result_row.get("container") or "").strip()
                config_row = config_rows.get(issue_id)

                if config_row is None:
                    unmatched += 1
                    continue
                
                matched += 1
                writer.writerow(config_row)
                
    print(f"Matched:   {matched}")
    print(f"Unmatched: {unmatched}")
    print(f"Output:    {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
