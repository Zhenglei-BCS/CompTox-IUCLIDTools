import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from iuclid_engine.classify import UNKNOWN, classify_by_code, classify_by_keywords, load_code_table
from iuclid_engine.i6z import LinkRegistry, build_i6z
from iuclid_engine.mapping import MappingConfig
from iuclid_engine.models import parse_column_name, template_from_oht_label
from iuclid_engine.picklists import DEFAULT_PHRASE_CATALOGUE, CodeList, PhraseCatalogue, PicklistResolver
from iuclid_engine.pipeline import convert

ROOT = Path(__file__).resolve().parents[1]
ESR = "ENDPOINT_STUDY_RECORD.RepeatedDoseToxicityOral.materials_and_methods"
SPECIES = f"{ESR}.test_animals.species.value"
ROUTE = f"{ESR}.administration_exposure.route_of_administration.value"
EXISTING_SUBSTANCE = "11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222"


@pytest.fixture(scope="module")
def catalogue():
    return PhraseCatalogue.load(DEFAULT_PHRASE_CATALOGUE)


def build(data, **kwargs):
    buffer = io.BytesIO()
    result = build_i6z(data, buffer, **kwargs)
    return result, zipfile.ZipFile(buffer)


def issues_of(result, category):
    return [i for i in result.issues if i.category == category]


def test_template_label_and_column_parsing():
    assert template_from_oht_label("OHT 67: Repeated Dose Toxicity Oral") == "RepeatedDoseToxicityOral"
    assert parse_column_name(SPECIES)[0] == "RepeatedDoseToxicityOral"
    with pytest.raises(ValueError):
        parse_column_name("not a path")


def test_picklist_labels_codelist_other_and_rejection(catalogue):
    codelist = CodeList(name="test", version="1", fields={"species": {"rattus norvegicus": "rat"}})
    data = pd.DataFrame({SPECIES: ["rat", "Rattus norvegicus", "chicken"],
                         ROUTE: ["oral: gavage", "oral: gavage", "by mouth somehow"]})
    result, archive = build(data, resolver=PicklistResolver([codelist], catalogue))

    assert result.records == 3
    assert len([i for i in issues_of(result, "translated_value") if "code list" in i.message]) == 1
    rejected = issues_of(result, "rejected_value")
    assert [(i.row, i.value) for i in rejected] == [(2, "chicken")]
    other = issues_of(result, "other_value")
    assert [i.value for i in other] == ["by mouth somehow"]

    names = archive.namelist()
    assert "manifest.xml" in names and "data.xlsx" in names
    documents = b"".join(result.i6d_contents.values())
    assert b"by mouth somehow" in documents
    assert b"chicken" not in documents


def test_link_registry_reuses_existing_substance():
    links = LinkRegistry({"Substance": {"key": "Substance.chemical_name",
                                        "documents": {"Known": EXISTING_SUBSTANCE}}})
    data = pd.DataFrame({"Substance.chemical_name": ["Known", "New"], SPECIES: ["4194", "4194"]})
    result, _ = build(data, links=links)

    assert result.reused_links == {"Substance": 1}
    created = [d for d in result.documents if d[1].upper() == "SUBSTANCE"]
    assert len(created) == 1
    assert not issues_of(result, "link")


def test_parent_substance_key_avoids_orphan_warning():
    data = pd.DataFrame({SPECIES: ["4194"]})
    result, _ = build(data, parent_substance_key=EXISTING_SUBSTANCE)
    assert not issues_of(result, "link")
    record_keys = [key for key, doc_type, _ in result.documents if doc_type == "ENDPOINT_STUDY_RECORD"]
    assert EXISTING_SUBSTANCE.encode() in result.i6d_contents[record_keys[0]]

    orphan, _ = build(data)
    assert len(issues_of(orphan, "link")) == 1


def test_mapping_validation(tmp_path):
    raw = {"mapping_id": "m", "version": "2", "template": "RepeatedDoseToxicityOral",
           "columns": {"a": SPECIES, "b": SPECIES,
                       "c": "ENDPOINT_STUDY_RECORD.AcuteToxicityOral.materials_and_methods.test_animals.species.value",
                       "d": "nonsense"}}
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(raw))
    mapping = MappingConfig.load(path)
    problems = mapping.validate()
    assert any("already mapped" in p for p in problems)
    assert any("not for template" in p for p in problems)
    assert any(p.startswith("d:") for p in problems)
    assert mapping.version == "2" and len(mapping.sha256) == 64


def test_classify_by_code_with_keyword_fallback(tmp_path):
    table_path = tmp_path / "table.csv"
    table_path.write_text("# comment\ncode,oht_class\nOECD 408,OHT 67: Repeated Dose Toxicity Oral\n")
    table = load_code_table(table_path)
    df = pd.DataFrame({"guideline": ["OECD 408", "XYZ", None],
                       "study_type": ["", "", ""], "exposure_route": ["", "", ""]})
    strict = classify_by_code(df, "guideline", table)
    assert list(strict["OHT_Class"]) == ["OHT 67: Repeated Dose Toxicity Oral", UNKNOWN, UNKNOWN]
    with_fallback = classify_by_code(df, "guideline", table, fallback_to_keywords=True)
    assert with_fallback["OHT_Class"].iloc[0] == "OHT 67: Repeated Dose Toxicity Oral"


def test_keyword_classification_matches_app_behaviour():
    df = pd.read_csv(ROOT / "test_files" / "oht67test.csv")
    assert set(classify_by_keywords(df)["OHT_Class"]) == {"OHT 67: Repeated Dose Toxicity Oral"}


def test_bundled_classification_table_uses_known_templates():
    table = load_code_table(ROOT / "pipeline_config" / "classification" / "oecd_guideline_to_oht.csv")
    for label in set(table.values()):
        assert template_from_oht_label(label)


@pytest.mark.parametrize("strict", [False, True])
def test_convert_writes_report_and_audit_log(tmp_path, strict):
    output = tmp_path / "out.i6z"
    report = convert(ROOT / "test_files" / "oht67test.csv",
                     ROOT / "pipeline_config" / "mappings" / "toxval" / "oht67_repeated_dose_oral.json",
                     output, operator="tester", strict=strict)

    assert report["counts"]["records"] == 20
    assert report["summary"]["rejected_values"] >= 1
    assert report["status"] == ("failed" if strict else "completed_with_errors")
    assert output.exists() != strict
    assert json.loads(output.with_suffix(".report.json").read_text())["status"] == report["status"]
    audit = [json.loads(line) for line in (tmp_path / "audit_log.jsonl").read_text().splitlines()]
    assert audit[-1]["operator"] == "tester"
    assert audit[-1]["mapping"]["mapping_id"] == "toxval_oht67_repeated_dose_oral"
