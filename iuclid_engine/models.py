"""Access to the xsdata-generated IUCLID entity models in ``entity_models``.

The generated packages import each other as top-level modules (for example
``ph_6_8.models``), so the ``entity_models`` directory has to be on
``sys.path`` before they are imported.
"""
import dataclasses
import importlib
import re
import sys
import typing
from enum import Enum
from pathlib import Path
from typing import Union

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTITY_MODELS_DIR = REPO_ROOT / "entity_models"
MODEL_PACKAGE_SUFFIX = "_6_8"

if str(ENTITY_MODELS_DIR) not in sys.path:
    sys.path.append(str(ENTITY_MODELS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

# Non-endpoint document types that can be produced next to endpoint study records.
LINKED_ENTITY_TYPES = ("TestMaterialInformation", "LegalEntity", "ReferenceSubstance", "Substance")


def camel_to_snake(name):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def snake_to_camel(name):
    return "".join([t.capitalize() for t in name.split('_')])


def to_document_type_format(oht_type):
    return "_".join(re.findall(r'[A-Z][^A-Z]*', oht_type)).upper()


# OHT Word file names that differ from the generated model names.
TEMPLATE_ALIASES = {
    "ShortTermToxToFisch": "ShortTermToxicityToFish",
    "ToxicityToTerrestrialArthropods": "ToxicityToTerrestrialArthropodsOtherThanBees",
    "BiodegradationInsoil": "BiodegradationInSoil",
}


def template_from_oht_label(oht_label):
    """Turn a UI label such as ``"OHT 67: Repeated Dose Toxicity Oral"`` into ``"RepeatedDoseToxicityOral"``."""
    parts = oht_label.split(":")
    template = parts[1].strip().replace(" ", "") if len(parts) > 1 else parts[0].strip()
    return TEMPLATE_ALIASES.get(template, template)


def parse_column_name(column_name: str):
    """Split a mapped IUCLID column name into (document type, field path list)."""
    if column_name.startswith("ENDPOINT_STUDY_RECORD."):
        match = re.match(r'ENDPOINT_STUDY_RECORD\.(\w+)\.(.+)', column_name)
        if match:
            return match.group(1), match.group(2).split('.')
    else:
        # ReferenceSubstance must be checked before Substance.
        for entity in ("ReferenceSubstance", "Substance", "TestMaterialInformation", "LegalEntity"):
            match = re.match(rf'{entity}\.(.+)', column_name)
            if match:
                return entity, match.group(1).split('.')
    raise ValueError(f"Invalid IUCLID column name: {column_name}")


def entity_kind(column_name: str):
    """Return the linked entity type of a mapped column, or ``"ENDPOINT_STUDY_RECORD"``."""
    oht_type, _ = parse_column_name(column_name)
    return oht_type if oht_type in LINKED_ENTITY_TYPES else "ENDPOINT_STUDY_RECORD"


def models_package(oht_type):
    return f"entity_models.{oht_type.lower()}{MODEL_PACKAGE_SUFFIX}.models"


def get_oht_classes(oht_type):
    module = importlib.import_module(models_package(oht_type))
    if oht_type in LINKED_ENTITY_TYPES:
        oht_class_name = oht_type
    else:
        oht_class_name = f"EndpointStudyRecord{oht_type}"
    oht_class = getattr(module, oht_class_name)
    nested_classes = {cls_name: getattr(module, cls_name) for cls_name in dir(module) if
                      cls_name.startswith(oht_class_name)}
    return oht_class, nested_classes


def list_available_templates():
    """Endpoint templates for which a generated model package exists."""
    templates = []
    for package in sorted(ENTITY_MODELS_DIR.glob(f"*{MODEL_PACKAGE_SUFFIX}")):
        init = package / "models" / "__init__.py"
        if not init.exists():
            continue
        match = re.search(r"\bEndpointStudyRecord([A-Za-z0-9]+?),", init.read_text(encoding="utf-8"))
        if match and models_package(match.group(1)).split(".")[1] == package.name:
            templates.append(match.group(1))
    return sorted(set(templates))


def get_actual_type(field_type):
    """Strip ``Optional[...]`` from a type annotation."""
    if typing.get_origin(field_type) is Union:
        for arg in typing.get_args(field_type):
            if arg is not type(None):
                return arg
    return field_type


def dataclass_field_type(instance_or_cls, field_name):
    if not dataclasses.is_dataclass(instance_or_cls):
        return None
    for f in dataclasses.fields(instance_or_cls):
        if f.name == field_name:
            return f.type
    return None


def enum_type_of(field_type):
    """Return the Enum class for a picklist ``value`` annotation, if any."""
    actual = get_actual_type(field_type)
    if isinstance(actual, type) and issubclass(actual, Enum):
        return actual
    return None


def get_model_fields(model, prefix=""):
    fields = []
    for field_name, field_type in model.__annotations__.items():
        full_path = f"{prefix}.{field_name}" if prefix else field_name
        actual_type = get_actual_type(field_type)
        if isinstance(actual_type, typing._GenericAlias):
            continue
        if hasattr(actual_type, "__annotations__") and hasattr(actual_type, "__name__"):
            if ("Substance" in actual_type.__name__ or "EndpointStudyRecord" in actual_type.__name__ or
                    "ReferenceSubstance" in actual_type.__name__ or
                    "TestMaterialInformation" in actual_type.__name__ or "LegalEntity" in actual_type.__name__):
                fields.extend(get_model_fields(actual_type, full_path))
            else:
                fields.append(full_path)
        else:
            fields.append(full_path)
    return fields


def load_and_introspect_models(model_names):
    unique_cols = set()
    for model_name in model_names:
        module = importlib.import_module(models_package(model_name))
        model_class = getattr(module, model_name)
        unique_cols.update(get_model_fields(model_class, model_name))
    return unique_cols


def initialize_column_mapping_options(oht_type, general_model_names=LINKED_ENTITY_TYPES):
    unique_cols = load_and_introspect_models(general_model_names)
    oht_class, _ = get_oht_classes(oht_type)
    unique_cols.update(get_model_fields(oht_class, f"ENDPOINT_STUDY_RECORD.{oht_type}"))
    return unique_cols
