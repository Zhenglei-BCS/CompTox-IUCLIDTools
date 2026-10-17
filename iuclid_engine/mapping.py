"""Managed column mappings.

Two JSON formats are accepted:

* the flat format downloaded from the Streamlit app
  (``{"source column": "IUCLID.path", ...}``);
* the managed format, which adds the metadata needed to version-control and
  review a mapping per source system and template::

    {
      "mapping_id": "lims-x_oht67",
      "version": "1.0.0",
      "source_system": "LIMS-X",
      "template": "RepeatedDoseToxicityOral",
      "reviewed_by": "...", "reviewed_on": "YYYY-MM-DD",
      "columns": {"source column": "IUCLID.path", ...},
      "constants": {"IUCLID.path": "fixed value", ...},
      "required_fields": ["IUCLID.path", ...],
      "codelists": ["relative/or/absolute/codelist.json", ...]
    }
"""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .models import parse_column_name


@dataclass
class MappingConfig:
    columns: Dict[str, str]
    mapping_id: str = ""
    version: str = ""
    source_system: str = ""
    template: Optional[str] = None
    reviewed_by: str = ""
    reviewed_on: str = ""
    constants: Dict[str, str] = field(default_factory=dict)
    required_fields: List[str] = field(default_factory=list)
    codelists: List[str] = field(default_factory=list)
    source: str = ""
    sha256: str = ""

    @classmethod
    def from_dict(cls, raw: dict, source: str = "", sha256: str = "", base_dir: Optional[Path] = None):
        if "columns" not in raw:
            if not all(isinstance(v, str) for v in raw.values()):
                raise ValueError("Mapping JSON must be a flat {column: IUCLID path} object or contain 'columns'")
            return cls(columns=dict(raw), source=source, sha256=sha256)
        codelists = []
        for path in raw.get("codelists", []):
            p = Path(path)
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            codelists.append(str(p))
        return cls(
            columns=dict(raw["columns"]),
            mapping_id=raw.get("mapping_id", ""),
            version=str(raw.get("version", "")),
            source_system=raw.get("source_system", ""),
            template=raw.get("template"),
            reviewed_by=raw.get("reviewed_by", ""),
            reviewed_on=raw.get("reviewed_on", ""),
            constants=dict(raw.get("constants", {})),
            required_fields=list(raw.get("required_fields", [])),
            codelists=codelists,
            source=source,
            sha256=sha256,
        )

    @classmethod
    def load(cls, path):
        path = Path(path)
        content = path.read_bytes()
        raw = json.loads(content.decode("utf-8"))
        return cls.from_dict(raw, source=str(path), sha256=hashlib.sha256(content).hexdigest(),
                             base_dir=path.parent)

    def validate(self, template: Optional[str] = None):
        """Return a list of problems with the mapping itself (bad paths, duplicates, wrong template)."""
        problems = []
        template = template or self.template
        seen = {}
        for src, target in list(self.columns.items()) + [("<constant>", t) for t in self.constants]:
            try:
                oht_type, _ = parse_column_name(target)
            except ValueError as e:
                problems.append(f"{src}: {e}")
                continue
            if target.startswith("ENDPOINT_STUDY_RECORD.") and template and oht_type != template:
                problems.append(f"{src}: path {target} is not for template {template}")
            if target in seen:
                problems.append(f"{src}: IUCLID path {target} is already mapped from {seen[target]}")
            seen[target] = src
        return problems

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a frame with only mapped columns, renamed to IUCLID paths, plus constant columns."""
        columns_to_keep = [col for col in df.columns if col in self.columns]
        mapped = df[columns_to_keep].rename(columns=self.columns)
        for target, value in self.constants.items():
            mapped[target] = value
        return mapped

    def unmapped_columns(self, df: pd.DataFrame) -> List[str]:
        return [col for col in df.columns if col not in self.columns]

    def missing_columns(self, df: pd.DataFrame) -> List[str]:
        return [col for col in self.columns if col not in df.columns]


def apply_column_mapping(column_mapping: dict, modified_df: pd.DataFrame) -> pd.DataFrame:
    return MappingConfig(columns=column_mapping).apply(modified_df)
