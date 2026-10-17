"""Validation of mapped data and generated documents."""
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from lxml import etree

from .i6z import NS_CONTAINER, Issue, is_empty

logger = logging.getLogger(__name__)


def check_required_fields(data: pd.DataFrame, required_fields: List[str]) -> List[Issue]:
    """Each required IUCLID path must be mapped and filled in every row."""
    issues = []
    for path in required_fields:
        if path not in data.columns:
            issues.append(Issue("error", "required_field", f"Required field {path} is not mapped", column=path))
            continue
        for index, value in data[path].items():
            if is_empty(value):
                issues.append(Issue("error", "required_field", f"Required field {path} is empty", index, path))
    return issues


class XsdValidator:
    """Validates i6d documents against the official IUCLID format XSDs.

    ``xsd_dir`` is the unpacked IUCLID format package for the target IUCLID
    version (the ``.xsd`` files published by ECHA). Schemas are found by their
    ``targetNamespace``.
    """

    def __init__(self, xsd_dir):
        self.xsd_dir = Path(xsd_dir)
        self._paths: Dict[str, Path] = {}
        self._schemas: Dict[str, Optional[etree.XMLSchema]] = {}
        for xsd in self.xsd_dir.rglob("*.xsd"):
            try:
                target = etree.parse(str(xsd)).getroot().get("targetNamespace")
            except etree.XMLSyntaxError:
                continue
            if target:
                self._paths.setdefault(target, xsd)
        logger.info("Indexed %d XSD namespaces in %s", len(self._paths), self.xsd_dir)

    def _schema(self, namespace):
        if namespace not in self._schemas:
            path = self._paths.get(namespace)
            self._schemas[namespace] = etree.XMLSchema(etree.parse(str(path))) if path else None
        return self._schemas[namespace]

    def validate(self, document_key: str, i6d_content: bytes) -> List[Issue]:
        root = etree.fromstring(i6d_content)
        content = root.find(f"{{{NS_CONTAINER}}}Content")
        if content is None or len(content) == 0:
            return [Issue("error", "schema", "Document has no content", document_key=document_key)]
        element = content[0]
        namespace = etree.QName(element).namespace
        schema = self._schema(namespace)
        if schema is None:
            return [Issue("warning", "schema", f"No XSD found for namespace {namespace}",
                          document_key=document_key)]
        if schema.validate(etree.ElementTree(element)):
            return []
        return [Issue("error", "schema", f"line {err.line}: {err.message}", document_key=document_key)
                for err in schema.error_log]
