"""Run reports and the audit trail."""
import datetime
import getpass
import hashlib
import json
import os
import socket
import subprocess
from collections import Counter
from pathlib import Path
from typing import List, Optional

from .i6z import Issue
from .models import REPO_ROOT


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version() -> str:
    """Git commit of the tool, or ``IUCLID_ENGINE_BUILD`` when running from a container image."""
    build = os.environ.get("IUCLID_ENGINE_BUILD")
    if build:
        return build
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def current_operator(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    if os.environ.get("IUCLID_ENGINE_OPERATOR"):
        return os.environ["IUCLID_ENGINE_OPERATOR"]
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def run_context(operator: Optional[str] = None) -> dict:
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "operator": current_operator(operator),
        "host": socket.gethostname(),
        "tool_version": tool_version(),
    }


def summarise_issues(issues: List[Issue]) -> dict:
    by_category = Counter(issue.category for issue in issues)
    by_severity = Counter(issue.severity for issue in issues)
    return {
        "errors": by_severity.get("error", 0),
        "warnings": by_severity.get("warning", 0),
        "rejected_values": by_category.get("rejected_value", 0),
        "other_values": by_category.get("other_value", 0),
        "translated_values": by_category.get("translated_value", 0),
        "missing_required_values": by_category.get("required_field", 0),
        "schema_errors": sum(1 for i in issues if i.category == "schema" and i.severity == "error"),
        "by_category": dict(by_category),
    }


def write_report(report: dict, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def append_audit_log(report: dict, path):
    """Append one JSON line per run. The log is append-only; keep it on storage the operator cannot rewrite."""
    entry = {
        **report["run"],
        "status": report["status"],
        "input": report["input"],
        "mapping": {k: report["mapping"].get(k) for k in ("source", "sha256", "mapping_id", "version")},
        "codelists": report.get("codelists", []),
        "template": report.get("template"),
        "output": report.get("output"),
        "report": report.get("report_path"),
        "summary": {k: v for k, v in report["summary"].items() if k != "by_category"},
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
