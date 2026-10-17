"""Command-line interface.

Examples::

    python -m iuclid_engine convert --input data.csv --mapping mappings/x.json --output out/x.i6z
    python -m iuclid_engine classify --input data.csv --code-column guideline \
        --table classification/oecd_guideline_to_oht.csv --split-dir out/split
    python -m iuclid_engine templates
    python -m iuclid_engine fields --template RepeatedDoseToxicityOral
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .picklists import DEFAULT_PHRASE_CATALOGUE


def _cmd_convert(args):
    from .pipeline import convert

    report = convert(
        args.input, args.mapping, args.output, template=args.template, codelist_paths=args.codelist,
        phrase_catalogue_path=None if args.no_phrase_catalogue else args.phrase_catalogue,
        links_path=args.links, parent_substance_key=args.parent_substance_key, xsd_dir=args.xsd_dir,
        settings_path=args.settings, attachment_paths=args.attachment, report_path=args.report,
        audit_log_path=args.audit_log, operator=args.operator, strict=args.strict,
        include_data_sheet=not args.no_data_sheet)
    print(json.dumps({"status": report["status"], "records": report["counts"]["records"],
                      "summary": {k: v for k, v in report["summary"].items() if k != "by_category"},
                      "output": report["output"], "report": report["report_path"]}, indent=2))
    return 0 if report["summary"]["errors"] == 0 else 2


def _cmd_classify(args):
    from .classify import UNKNOWN, classify_by_code, classify_by_keywords, load_code_table
    from .pipeline import read_table

    df = read_table(args.input)
    if args.table:
        if not args.code_column:
            raise SystemExit("--code-column is required with --table")
        result = classify_by_code(df, args.code_column, load_code_table(args.table),
                                  fallback_to_keywords=args.keyword_fallback)
    else:
        result = classify_by_keywords(df)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, index=False)
    if args.split_dir:
        split_dir = Path(args.split_dir)
        split_dir.mkdir(parents=True, exist_ok=True)
        for label, group in result.groupby("OHT_Class"):
            name = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
            group.drop(columns=[c for c in ("OHT_Class", "Classification_Reasoning", "Calculated_Study_Duration")
                                if c in group.columns]).to_csv(split_dir / f"{name}.csv", index=False)
    counts = result["OHT_Class"].value_counts().to_dict()
    print(json.dumps({"rows": len(result), "classes": counts}, indent=2))
    return 0 if UNKNOWN not in counts else 2


def _cmd_templates(_args):
    from .models import list_available_templates

    print("\n".join(list_available_templates()))
    return 0


def _cmd_fields(args):
    from .models import initialize_column_mapping_options

    print("\n".join(sorted(initialize_column_mapping_options(args.template))))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m iuclid_engine", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("convert", help="Convert a table to an .i6z using a saved mapping")
    p.add_argument("--input", required=True, help="CSV or XLSX in the standard tabular format")
    p.add_argument("--mapping", required=True, help="Mapping JSON (flat or managed format)")
    p.add_argument("--output", required=True, help="Path of the .i6z to write")
    p.add_argument("--template", help="Endpoint template, e.g. RepeatedDoseToxicityOral (default: from mapping)")
    p.add_argument("--codelist", action="append", default=[], help="Extra code-list JSON (repeatable)")
    p.add_argument("--phrase-catalogue", default=str(DEFAULT_PHRASE_CATALOGUE),
                   help="IUCLID field definition workbook with sheet I6Picklists (default: bundled 6.6 file)")
    p.add_argument("--no-phrase-catalogue", action="store_true", help="Do not resolve picklist labels")
    p.add_argument("--links", help="Link registry JSON with existing IUCLID document keys to reuse")
    p.add_argument("--parent-substance-key", help="Existing IUCLID substance document key owning the records")
    p.add_argument("--xsd-dir", help="Unpacked IUCLID format XSDs for schema validation")
    p.add_argument("--settings", help="Format settings JSON (IUCLID/definition/legislation versions)")
    p.add_argument("--attachment", action="append", default=[], help="PDF/PNG/JPG to attach (repeatable)")
    p.add_argument("--report", help="Run report path (default: <output>.report.json)")
    p.add_argument("--audit-log", help="Append-only audit log (default: <output dir>/audit_log.jsonl)")
    p.add_argument("--operator", help="Person/service account running the conversion")
    p.add_argument("--strict", action="store_true", help="Do not write the .i6z if any error is found")
    p.add_argument("--no-data-sheet", action="store_true", help="Do not embed the mapped data as data.xlsx")
    p.set_defaults(func=_cmd_convert)

    p = sub.add_parser("classify", help="Assign rows to OHTs and optionally split per template")
    p.add_argument("--input", required=True)
    p.add_argument("--code-column", help="Column holding the study-type code or guideline")
    p.add_argument("--table", help="CSV with columns code,oht_class")
    p.add_argument("--keyword-fallback", action="store_true", help="Use keyword rules for codes not in table")
    p.add_argument("--output", help="CSV with OHT_Class and reasoning columns added")
    p.add_argument("--split-dir", help="Write one CSV per OHT class into this directory")
    p.set_defaults(func=_cmd_classify)

    p = sub.add_parser("templates", help="List endpoint templates with generated models")
    p.set_defaults(func=_cmd_templates)

    p = sub.add_parser("fields", help="List mappable IUCLID paths for a template")
    p.add_argument("--template", required=True)
    p.set_defaults(func=_cmd_fields)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
