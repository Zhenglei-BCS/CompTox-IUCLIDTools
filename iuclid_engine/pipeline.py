"""End-to-end conversion: input table + mapping JSON + template -> validated .i6z + report + audit entry."""
import io
import logging
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

from .i6z import FormatSettings, Issue, LinkRegistry, build_i6z
from .mapping import MappingConfig
from .picklists import DEFAULT_PHRASE_CATALOGUE, CodeList, PhraseCatalogue, PicklistResolver
from .report import append_audit_log, run_context, sha256_file, summarise_issues, write_report
from .validation import XsdValidator, check_required_fields

logger = logging.getLogger(__name__)


def read_table(path, sheet_name=0) -> pd.DataFrame:
    """Read the standard tabular input (CSV or XLSX). All cells are read as text."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, sheet_name=sheet_name, dtype=str)
    raise ValueError(f"Unsupported file format: {path.suffix}")


def convert(input_path, mapping_path, output_path, *, template: Optional[str] = None,
            codelist_paths: Iterable = (), phrase_catalogue_path=DEFAULT_PHRASE_CATALOGUE,
            links_path=None, parent_substance_key: Optional[str] = None, xsd_dir=None,
            settings_path=None, attachment_paths: Iterable = (), report_path=None, audit_log_path=None,
            operator: Optional[str] = None, strict: bool = False, include_data_sheet: bool = True) -> dict:
    """Run one conversion and return the run report (also written to ``report_path``)."""
    output_path = Path(output_path)
    report_path = Path(report_path) if report_path else output_path.with_suffix(".report.json")
    audit_log_path = Path(audit_log_path) if audit_log_path else output_path.parent / "audit_log.jsonl"
    context = run_context(operator)
    logger.info("Run started by %s (tool %s)", context["operator"], context["tool_version"])

    mapping = MappingConfig.load(mapping_path)
    template = template or mapping.template
    if not template:
        raise ValueError("No template given: pass --template or set 'template' in the mapping JSON")

    df = read_table(input_path)
    logger.info("Read %d rows x %d columns from %s", len(df), len(df.columns), input_path)

    codelists: List[CodeList] = [CodeList.load(p) for p in list(mapping.codelists) + list(codelist_paths)]
    catalogue = None
    if phrase_catalogue_path and Path(phrase_catalogue_path).exists():
        logger.info("Loading phrase catalogue %s", phrase_catalogue_path)
        catalogue = PhraseCatalogue.load(phrase_catalogue_path)
    resolver = PicklistResolver(codelists, catalogue)
    links = LinkRegistry.load(links_path) if links_path else LinkRegistry()
    settings = FormatSettings.load(settings_path) if settings_path else FormatSettings()

    issues: List[Issue] = [Issue("error", "mapping", p) for p in mapping.validate(template)]
    missing = mapping.missing_columns(df)
    for col in missing:
        issues.append(Issue("warning", "mapping", f"Mapped source column {col} is not in the input", column=col))

    mapped = mapping.apply(df)
    issues += check_required_fields(mapped, mapping.required_fields)

    attachments = [(Path(p).name, Path(p).read_bytes()) for p in attachment_paths]
    buffer = io.BytesIO()
    result = build_i6z(mapped, buffer, settings=settings, resolver=resolver, links=links,
                       parent_substance_key=parent_substance_key, attachments=attachments,
                       include_data_sheet=include_data_sheet)
    issues += result.issues

    if xsd_dir:
        validator = XsdValidator(xsd_dir)
        for document_key, content in result.i6d_contents.items():
            issues += validator.validate(document_key, content)

    summary = summarise_issues(issues)
    if summary["errors"] and strict:
        status = "failed"
    elif summary["errors"]:
        status = "completed_with_errors"
    elif summary["warnings"]:
        status = "completed_with_warnings"
    else:
        status = "completed"

    output = None
    if status != "failed":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(buffer.getvalue())
        output = {"path": str(output_path), "sha256": sha256_file(output_path)}
        logger.info("Wrote %s", output_path)
    else:
        logger.error("Strict mode: %d errors, no i6z written", summary["errors"])

    documents_by_type = {}
    for _, doc_type, _ in result.documents:
        documents_by_type[doc_type] = documents_by_type.get(doc_type, 0) + 1

    report = {
        "status": status,
        "run": context,
        "input": {"path": str(input_path), "sha256": sha256_file(input_path), "rows": len(df),
                  "columns": len(df.columns)},
        "mapping": {"source": mapping.source, "sha256": mapping.sha256, "mapping_id": mapping.mapping_id,
                    "version": mapping.version, "source_system": mapping.source_system,
                    "reviewed_by": mapping.reviewed_by, "reviewed_on": mapping.reviewed_on},
        "codelists": [{"name": c.name, "version": c.version, "source": c.source,
                       "sha256": sha256_file(c.source)} for c in codelists],
        "phrase_catalogue": catalogue.source if catalogue else None,
        "links": links.source or None,
        "parent_substance_key": parent_substance_key,
        "xsd_dir": str(xsd_dir) if xsd_dir else None,
        "format_settings": vars(settings),
        "template": template,
        "counts": {"records": result.records, "documents": documents_by_type,
                   "reused_linked_documents": result.reused_links, "attachments": len(attachments)},
        "unmapped_columns": mapping.unmapped_columns(df),
        "missing_mapped_columns": missing,
        "summary": summary,
        "issues": [issue.to_dict() for issue in issues],
        "output": output,
        "report_path": str(report_path),
    }
    write_report(report, report_path)
    append_audit_log(report, audit_log_path)
    logger.info("Status %s: %d records, %d errors, %d warnings (report %s)", status, result.records,
                summary["errors"], summary["warnings"], report_path)
    return report
