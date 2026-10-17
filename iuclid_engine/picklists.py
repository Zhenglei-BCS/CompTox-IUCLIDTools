"""Picklist (phrase) resolution and code-list translation.

IUCLID picklist fields store phrase *codes* (for example ``"4194"``), not the
text shown in the UI. Source systems store their own vocabulary
("Rattus norvegicus", "RAT", ...). A value is resolved in this order:

1. it already is a valid code for the field;
2. it is translated by a code-list table (source vocabulary -> IUCLID label or code);
3. it (or the translation) matches a phrase label in the IUCLID phrase catalogue;
4. otherwise, if the picklist is open, it is stored as ``"other:"`` plus free text;
5. otherwise it is rejected and reported.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .models import REPO_ROOT

DEFAULT_PHRASE_CATALOGUE = REPO_ROOT / "data" / "IUCLID6_6_all_fields_update01.xlsx"
DEFAULT_OTHER_CODE = "1342"  # phrase "other:" in IUCLID


def _norm_group(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(name)).upper()


def _norm_text(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


@dataclass
class Resolution:
    status: str  # "code", "translated", "label", "other", "rejected"
    code: Optional[str] = None
    other_text: Optional[str] = None
    message: str = ""


@dataclass
class CodeList:
    name: str
    version: str
    other_code: str = DEFAULT_OTHER_CODE
    # field key (full IUCLID path without ".value", or last path segment, or "*") -> {source term: label/code}
    fields: Dict[str, Dict[str, str]] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def load(cls, path):
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = {key: {_norm_text(k): str(v) for k, v in terms.items()}
                  for key, terms in raw.get("fields", {}).items()}
        return cls(name=raw.get("name", path.stem), version=str(raw.get("version", "")),
                   other_code=str(raw.get("other_code", DEFAULT_OTHER_CODE)), fields=fields, source=str(path))

    def lookup(self, field_path: str, term) -> Optional[str]:
        key_full = field_path[:-len(".value")] if field_path.endswith(".value") else field_path
        last = key_full.split(".")[-1]
        needle = _norm_text(term)
        for key in (key_full, last, "*"):
            terms = self.fields.get(key)
            if terms and needle in terms:
                return terms[needle]
        return None


class PhraseCatalogue:
    """Phrase group -> {label: code} from the IUCLID field definition workbook (sheet ``I6Picklists``)."""

    def __init__(self, groups: Dict[str, Dict[str, str]], source: str = ""):
        self.groups = groups
        self.source = source

    @classmethod
    def load(cls, path=DEFAULT_PHRASE_CATALOGUE):
        import pandas as pd

        path = Path(path)
        df = pd.read_excel(path, sheet_name="I6Picklists", usecols=["phrasegroup", "phraseid", "phrasetext"])
        groups: Dict[str, Dict[str, str]] = {}
        for group, code, text in df.itertuples(index=False):
            if pd.isna(group) or pd.isna(code) or pd.isna(text):
                continue
            code = str(int(code)) if isinstance(code, float) else str(code)
            groups.setdefault(_norm_group(group), {}).setdefault(_norm_text(text), code)
        return cls(groups, source=str(path))

    def code_for(self, enum_cls, label) -> Optional[str]:
        group = self.groups.get(_norm_group(enum_cls.__name__))
        if not group:
            return None
        return group.get(_norm_text(label))


class PicklistResolver:
    def __init__(self, codelists: Optional[List[CodeList]] = None, catalogue: Optional[PhraseCatalogue] = None,
                 other_code: str = DEFAULT_OTHER_CODE):
        self.codelists = codelists or []
        self.catalogue = catalogue
        self.other_code = other_code

    @staticmethod
    def _valid_codes(enum_cls):
        return {member.value for member in enum_cls if member.value != ""}

    def _to_code(self, enum_cls, text, valid):
        text = str(text).strip()
        if text in valid:
            return text
        if self.catalogue is not None:
            code = self.catalogue.code_for(enum_cls, text)
            if code in valid:
                return code
        return None

    def resolve(self, field_path: str, enum_cls, raw_value, has_other_field: bool) -> Resolution:
        valid = self._valid_codes(enum_cls)
        text = str(raw_value).strip()
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]  # numeric codes read from spreadsheets as floats
        if text in valid:
            return Resolution("code", code=text)

        for codelist in self.codelists:
            translated = codelist.lookup(field_path, text)
            if translated is not None:
                code = self._to_code(enum_cls, translated, valid)
                if code:
                    return Resolution("translated", code=code,
                                      message=f"'{text}' -> '{translated}' via code list {codelist.name}")
                return Resolution("rejected", message=(
                    f"code list {codelist.name} translates '{text}' to '{translated}', "
                    f"which is not a valid phrase for picklist {enum_cls.__name__}"))

        code = self._to_code(enum_cls, text, valid)
        if code:
            return Resolution("label", code=code, message=f"'{text}' matched phrase label")

        other_codes = [c.other_code for c in self.codelists] + [self.other_code]
        other = next((c for c in other_codes if c in valid), None)
        if other and has_other_field:
            return Resolution("other", code=other, other_text=text,
                              message=f"'{text}' not found in picklist {enum_cls.__name__}; stored as 'other:'")
        return Resolution("rejected", message=f"'{text}' is not a valid phrase for picklist {enum_cls.__name__}")
