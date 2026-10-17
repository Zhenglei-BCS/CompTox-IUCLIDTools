"""Build IUCLID documents from mapped rows and write them to an ``.i6z`` archive.

Nothing in this module depends on Streamlit: the archive is written into a
binary buffer supplied by the caller (``io.BytesIO`` or an open file).
"""
import dataclasses
import datetime
import hashlib
import io
import json
import logging
import uuid
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from lxml import etree
from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.serializers import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

from .models import (LINKED_ENTITY_TYPES, camel_to_snake, dataclass_field_type, entity_kind, enum_type_of,
                     get_actual_type, get_oht_classes, models_package, parse_column_name, snake_to_camel,
                     to_document_type_format)
from .picklists import PicklistResolver

logger = logging.getLogger(__name__)

EMPTY_VALUES = (None, 'None', 'none', 'NA', 'na', '')
NS_CONTAINER = "http://iuclid6.echa.europa.eu/namespaces/platform-container/v2"
NS_METADATA = "http://iuclid6.echa.europa.eu/namespaces/platform-metadata/v1"
NS_MANIFEST = "http://iuclid6.echa.europa.eu/namespaces/manifest/v1"
NS_XLINK = "http://www.w3.org/1999/xlink"
TEST_MATERIAL_LINK_PATH = ['MaterialsAndMethods', 'TestMaterials', 'TestMaterialInformation']


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def generate_uuid():
    return str(uuid.uuid4())


def is_empty(value):
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return value in EMPTY_VALUES


@dataclass
class FormatSettings:
    """IUCLID version information written into each document and the manifest.

    The defaults reproduce the values the Streamlit app always used. Set them to
    match the IUCLID instance that will import the file.
    """
    iuclid_version: str = "7.0.7"
    definition_version: str = "8.0"
    namespace_version: str = "9.0"
    legislations: Dict[str, str] = field(default_factory=lambda: {"domain": "8.0", "core": "8.0", "oecd": "8.0"})
    author: str = "EZ Mapper"
    application: str = "IUCLID6 (EZ Mapper Export)"

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class Issue:
    severity: str  # "error", "warning", "info"
    category: str  # "rejected_value", "other_value", "translated_value", "required_field", "path", "link", "schema"
    message: str
    row: Optional[int] = None
    column: Optional[str] = None
    value: Optional[str] = None
    document_key: Optional[str] = None

    def to_dict(self):
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


class LinkRegistry:
    """Existing IUCLID document keys to reuse instead of creating new documents.

    JSON format::

        {"Substance": {"key": "Substance.chemical_name",
                       "documents": {"Glyphosate": "<uuid>/<uuid>"}},
         "LegalEntity": {...}, "ReferenceSubstance": {...}, "TestMaterialInformation": {...}}

    ``key`` is the mapped IUCLID column whose value identifies the entity.
    """

    def __init__(self, entries: Optional[dict] = None, source: str = ""):
        self.entries = entries or {}
        self.source = source

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), source=str(path))

    def lookup(self, entity_type: str, values: Dict[str, object]) -> Optional[str]:
        entry = self.entries.get(entity_type)
        if not entry:
            return None
        key_value = values.get(entry.get("key"))
        if is_empty(key_value):
            return None
        return entry.get("documents", {}).get(str(key_value).strip())


class RecordBuilder:
    """Creates model instances and sets field values, resolving picklists and recording issues."""

    def __init__(self, resolver: Optional[PicklistResolver] = None, issues: Optional[List[Issue]] = None):
        self.resolver = resolver or PicklistResolver()
        self.issues = issues if issues is not None else []

    def _descend(self, current, name):
        field_type = dataclass_field_type(current, name)
        if field_type is None:
            raise AttributeError(f"Attribute {name} not found in {type(current).__name__}")
        child = getattr(current, name)
        actual = get_actual_type(field_type)
        if isinstance(child, list):
            if not child:
                element_type = get_actual_type(actual.__args__[0])
                child.append(element_type())
            return child[-1]
        if child is None:
            child = actual()
            setattr(current, name, child)
        return child

    def set_field(self, instance, field_path, value, row=None, column=None):
        names = [camel_to_snake(part) for part in field_path]
        current = instance
        for name in names[:-1]:
            current = self._descend(current, name)
        final = names[-1]
        field_type = dataclass_field_type(current, final)
        if field_type is None:
            raise AttributeError(f"Attribute {final} not found in {type(current).__name__}")

        enum_cls = enum_type_of(field_type)
        if enum_cls is None:
            setattr(current, final, value)
            return True

        other_type = dataclass_field_type(current, "other")
        resolution = self.resolver.resolve(".".join(field_path), enum_cls, value, other_type is not None)
        if resolution.status == "rejected":
            self.issues.append(Issue("error", "rejected_value", resolution.message, row, column, str(value)))
            return False
        setattr(current, final, enum_cls(resolution.code))
        if resolution.status == "other":
            element_type = get_actual_type(get_actual_type(other_type).__args__[0])
            getattr(current, "other").append(element_type(value=resolution.other_text))
            self.issues.append(Issue("warning", "other_value", resolution.message, row, column, str(value)))
        elif resolution.status in ("translated", "label"):
            self.issues.append(Issue("info", "translated_value", resolution.message, row, column, str(value)))
        return True

    def build(self, oht_type, row_data: Dict[str, object], row=None):
        oht_class, _ = get_oht_classes(oht_type)
        instance = oht_class()
        for column_name, value in row_data.items():
            if is_empty(value):
                continue
            _, field_path = parse_column_name(column_name)
            try:
                self.set_field(instance, field_path, value, row=row, column=column_name)
            except (AttributeError, TypeError, ValueError) as e:
                self.issues.append(Issue("error", "path", str(e), row, column_name, str(value)))
        return instance


@lru_cache(maxsize=None)
def _serializer(oht_type):
    context = XmlContext(models_package=models_package(oht_type))
    context.build_recursive(get_oht_classes(oht_type)[0])
    config = SerializerConfig(pretty_print=True, xml_declaration=True, ignore_default_attributes=True)
    return XmlSerializer(config=config, context=context)


class I6zWriter:
    """Writes i6d documents, attachments, the source data sheet and the manifest into ``buffer``."""

    def __init__(self, buffer, main_uuid: str, settings: Optional[FormatSettings] = None):
        self.buffer = buffer
        self.main_uuid = main_uuid
        self.settings = settings or FormatSettings()
        self.documents: List[Tuple[str, str, str]] = []  # (document key, type, subtype)
        self.i6d_contents: Dict[str, bytes] = {}

    def _write(self, name, content: bytes):
        with zipfile.ZipFile(self.buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            zip_file.writestr(name, content)

    def _platform_metadata(self, document_key, parent_key, document_type, document_subtype):
        meta = etree.Element(f"{{{NS_CONTAINER}}}PlatformMetadata", nsmap={"i6c": NS_CONTAINER, "i6m": NS_METADATA})
        now = utc_now()
        values = [
            ("iuclidVersion", self.settings.iuclid_version),
            ("documentKey", document_key),
            ("parentDocumentKey", parent_key or ""),
            ("name", ""),
            ("documentType", document_type),
            ("documentSubType", document_subtype),
            ("orderInSectionNo", "1"),
            ("definitionVersion", self.settings.definition_version),
            ("creationDate", now),
            ("lastModificationDate", now),
            ("submissionType", ""),
            ("submissionTypeVersion", ""),
            ("submittingLegalEntity", ""),
            ("dossierSubject", ""),
            ("i5Origin", "false"),
            ("creationTool", "IUC6"),
            ("snapshotCreationTool", "IUC6"),
        ]
        for tag, text in values:
            etree.SubElement(meta, f"{{{NS_METADATA}}}{tag}").text = text
        return meta

    def add_document(self, instance, oht_type, document_key, parent_key=None):
        xml_content = _serializer(oht_type).render(instance, {
            None: f"http://iuclid6.echa.europa.eu/namespaces/ENDPOINT_STUDY_RECORD-{oht_type}/"
                  f"{self.settings.namespace_version}",
            "i6": "http://iuclid6.echa.europa.eu/namespaces/platform-fields/v1",
        })
        xml_content = xml_content.split("?>", 1)[1].strip()

        if oht_type in LINKED_ENTITY_TYPES:
            document_type, document_subtype = to_document_type_format(oht_type), ""
        else:
            document_type = "ENDPOINT_STUDY_RECORD"
            document_subtype = snake_to_camel(to_document_type_format(oht_type))

        ns_map = {
            None: f"http://iuclid6.echa.europa.eu/namespaces/ENDPOINT_STUDY_RECORD-{oht_type}/"
                  f"{self.settings.namespace_version}",
            "i6c": NS_CONTAINER,
            "xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xml": "http://www.w3.org/XML/1998/namespace",
        }
        root = etree.Element(f"{{{NS_CONTAINER}}}Document", nsmap=ns_map)
        root.append(self._platform_metadata(document_key, parent_key, document_type, document_subtype))
        content_element = etree.SubElement(root, f"{{{NS_CONTAINER}}}Content")
        content_element.append(etree.fromstring(xml_content.encode("utf-8")))

        content = etree.tostring(etree.ElementTree(root), pretty_print=True, xml_declaration=True, encoding="UTF-8")
        file_name = f"{document_key.replace('/', '_')}.i6d"
        self._write(file_name, content)
        self.documents.append((document_key, document_type, document_subtype))
        self.i6d_contents[document_key] = content
        return file_name

    def add_attachment(self, name: str, content: bytes):
        file_name = Path(name)
        mime_types = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        mime_type = mime_types.get(file_name.suffix.lower())
        if mime_type is None:
            raise ValueError(f"Unsupported file type: {file_name.suffix}")
        md5_hash = hashlib.md5(content).hexdigest()
        document_key = f"{generate_uuid()}/{self.main_uuid}"
        creation_date = utc_now()
        root = etree.Element("Attachment", nsmap={
            None: "http://iuclid6.echa.europa.eu/namespaces/platform-attachment/v1",
            "xlink": NS_XLINK,
            "xsi": "http://www.w3.org/2001/XMLSchema-instance",
        })
        etree.SubElement(root, 'documentKey').text = document_key
        etree.SubElement(root, "name").text = file_name.name
        etree.SubElement(root, "creationDate").text = creation_date
        etree.SubElement(root, "lastModificationDate").text = creation_date
        etree.SubElement(root, "md5").text = md5_hash
        etree.SubElement(root, "mimetype").text = mime_type
        attachment_filename = f"attachments/{md5_hash}{file_name.suffix}"
        etree.SubElement(root, "content", {f"{{{NS_XLINK}}}href": attachment_filename,
                                           f"{{{NS_XLINK}}}type": "simple"})
        self._write(attachment_filename, content)
        self._write(f"{document_key.replace('/', '_')}.i6d",
                    etree.tostring(etree.ElementTree(root), pretty_print=True, xml_declaration=True,
                                   encoding="UTF-8"))
        return document_key

    def add_data_sheet(self, data: pd.DataFrame):
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            data.to_excel(writer, sheet_name='Sheet1', index=False)
        self._write("data.xlsx", excel_buffer.getvalue())

    def write_manifest(self, base_document_key: Optional[str] = None):
        root = etree.Element(f"{{{NS_MANIFEST}}}manifest", nsmap={None: NS_MANIFEST, "xlink": NS_XLINK})
        general_info = etree.SubElement(root, f"{{{NS_MANIFEST}}}general-information")
        etree.SubElement(general_info, f"{{{NS_MANIFEST}}}title").text = "IUCLID 6 container manifest file"
        etree.SubElement(general_info, f"{{{NS_MANIFEST}}}created").text = utc_now()
        etree.SubElement(general_info, f"{{{NS_MANIFEST}}}author").text = self.settings.author
        etree.SubElement(general_info, f"{{{NS_MANIFEST}}}application").text = self.settings.application
        etree.SubElement(general_info, f"{{{NS_MANIFEST}}}submission-type").text = "EXPERIMENTAL_DATA"
        etree.SubElement(general_info, f"{{{NS_MANIFEST}}}archive-type").text = "DOSSIER_DATA"
        legislation_list = etree.SubElement(general_info, f"{{{NS_MANIFEST}}}legislations-info")
        for legislation_id, version in self.settings.legislations.items():
            legislation = etree.SubElement(legislation_list, f"{{{NS_MANIFEST}}}legislation")
            etree.SubElement(legislation, f"{{{NS_MANIFEST}}}id").text = legislation_id
            etree.SubElement(legislation, f"{{{NS_MANIFEST}}}version").text = version

        etree.SubElement(root, f"{{{NS_MANIFEST}}}base-document-uuid").text = (
            base_document_key or f"{self.main_uuid}/{self.main_uuid}")

        contained_docs = etree.SubElement(root, f"{{{NS_MANIFEST}}}contained-documents")
        for document_key, doc_type, doc_subtype in self.documents:
            file_name = f"{document_key.replace('/', '_')}.i6d"
            mod_time = utc_now()
            doc_elem = etree.SubElement(contained_docs, f"{{{NS_MANIFEST}}}document", id=document_key)
            etree.SubElement(doc_elem, f"{{{NS_MANIFEST}}}type").text = doc_type or "DOSSIER"
            if doc_subtype and doc_subtype.strip():
                etree.SubElement(doc_elem, f"{{{NS_MANIFEST}}}subtype").text = doc_subtype
            name_elem = etree.SubElement(doc_elem, f"{{{NS_MANIFEST}}}name")
            name_elem.text = file_name
            name_elem.attrib[f"{{{NS_XLINK}}}type"] = "simple"
            name_elem.attrib[f"{{{NS_XLINK}}}href"] = file_name
            etree.SubElement(doc_elem, f"{{{NS_MANIFEST}}}first-modification-date").text = mod_time
            etree.SubElement(doc_elem, f"{{{NS_MANIFEST}}}last-modification-date").text = mod_time
            etree.SubElement(doc_elem, f"{{{NS_MANIFEST}}}uuid").text = document_key

        self._write("manifest.xml", etree.tostring(etree.ElementTree(root), pretty_print=True,
                                                   xml_declaration=True, encoding="UTF-8"))


@dataclass
class BuildResult:
    main_uuid: str
    issues: List[Issue]
    documents: List[Tuple[str, str, str]]
    i6d_contents: Dict[str, bytes]
    records: int = 0
    reused_links: Dict[str, int] = field(default_factory=dict)


def _row_values(row, columns):
    return {col: row[col] for col in columns if col in row.index}


def _entity_key(row, columns):
    return tuple(None if is_empty(row[c]) else str(row[c]) for c in columns)


def build_i6z(data: pd.DataFrame, buffer, *, settings: Optional[FormatSettings] = None,
              resolver: Optional[PicklistResolver] = None, links: Optional[LinkRegistry] = None,
              parent_substance_key: Optional[str] = None, attachments=None, include_data_sheet: bool = True,
              main_uuid: Optional[str] = None, parent_uuid: Optional[str] = None) -> BuildResult:
    """Convert a frame whose columns are IUCLID paths into an i6z archive written to ``buffer``.

    ``attachments`` is an iterable of ``(file name, bytes)`` tuples or objects with
    ``name`` and ``getvalue()`` (such as Streamlit uploads).
    ``parent_substance_key`` attaches all endpoint study records to an existing
    substance; otherwise the substance created from mapped ``Substance.*``
    columns is used, falling back to a generated parent key.
    """
    main_uuid = main_uuid or generate_uuid()
    parent_uuid = parent_uuid or generate_uuid()
    links = links or LinkRegistry()
    issues: List[Issue] = []
    builder = RecordBuilder(resolver, issues)
    writer = I6zWriter(buffer, main_uuid, settings)
    reused: Dict[str, int] = {}
    legacy_parent = f"{parent_uuid}/{main_uuid}"

    columns_by_kind: Dict[str, List[str]] = {}
    for col in data.columns:
        columns_by_kind.setdefault(entity_kind(col), []).append(col)

    # Linked entities: one document per unique combination of mapped values.
    key_maps: Dict[str, Dict[tuple, str]] = {}
    created_substance_keys = []
    for entity_type in ("TestMaterialInformation", "LegalEntity", "ReferenceSubstance", "Substance"):
        columns = columns_by_kind.get(entity_type)
        if not columns:
            continue
        key_map = key_maps.setdefault(entity_type, {})
        for _, row in data[columns].drop_duplicates().iterrows():
            values = _row_values(row, columns)
            if all(is_empty(v) for v in values.values()):
                continue
            existing = links.lookup(entity_type, values)
            if existing:
                key_map[_entity_key(row, columns)] = existing
                reused[entity_type] = reused.get(entity_type, 0) + 1
                continue
            instance = builder.build(entity_type, values)
            document_key = f"{generate_uuid()}/{main_uuid}"
            writer.add_document(instance, entity_type, document_key, parent_key=legacy_parent)
            key_map[_entity_key(row, columns)] = document_key
            if entity_type == "Substance":
                created_substance_keys.append(document_key)

    base_document_key = created_substance_keys[0] if len(created_substance_keys) == 1 else None

    records = 0
    orphan_records = 0
    endpoint_columns = columns_by_kind.get("ENDPOINT_STUDY_RECORD", [])
    if endpoint_columns:
        oht_type, _ = parse_column_name(endpoint_columns[0])
        for index, row in data.iterrows():
            instance = builder.build(oht_type, _row_values(row, endpoint_columns), row=index)
            document_key = f"{generate_uuid()}/{main_uuid}"

            tm_columns = columns_by_kind.get("TestMaterialInformation")
            if tm_columns:
                tm_key = key_maps.get("TestMaterialInformation", {}).get(_entity_key(row, tm_columns))
                if tm_key:
                    try:
                        builder.set_field(instance, TEST_MATERIAL_LINK_PATH, tm_key, row=index)
                    except AttributeError as e:
                        issues.append(Issue("warning", "link", f"Cannot link test material: {e}", index))

            parent_key = parent_substance_key
            if parent_key is None and columns_by_kind.get("Substance"):
                parent_key = key_maps.get("Substance", {}).get(
                    _entity_key(row, columns_by_kind["Substance"]))
            if parent_key is None:
                parent_key = legacy_parent
                orphan_records += 1
            writer.add_document(instance, oht_type, document_key, parent_key=parent_key)
            records += 1

    if orphan_records:
        issues.append(Issue("warning", "link", f"{orphan_records} endpoint study record(s) have no owning substance; "
                            "pass a parent substance key or map Substance columns"))

    for attachment in attachments or []:
        if isinstance(attachment, tuple):
            name, content = attachment
        else:
            name, content = attachment.name, attachment.getvalue()
        try:
            writer.add_attachment(name, content)
        except ValueError as e:
            issues.append(Issue("error", "attachment", f"{name}: {e}"))

    writer.write_manifest(base_document_key)
    if include_data_sheet:
        writer.add_data_sheet(data)

    return BuildResult(main_uuid=main_uuid, issues=issues, documents=writer.documents,
                       i6d_contents=writer.i6d_contents, records=records, reused_links=reused)
