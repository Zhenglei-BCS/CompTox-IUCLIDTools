"""Streamlit-free conversion engine: tabular data + mapping JSON + template -> IUCLID 6 .i6z."""
from .i6z import FormatSettings, I6zWriter, Issue, LinkRegistry, RecordBuilder, build_i6z
from .mapping import MappingConfig, apply_column_mapping
from .picklists import CodeList, PhraseCatalogue, PicklistResolver

__all__ = [
    "FormatSettings", "I6zWriter", "Issue", "LinkRegistry", "RecordBuilder", "build_i6z",
    "MappingConfig", "apply_column_mapping", "CodeList", "PhraseCatalogue", "PicklistResolver",
]
