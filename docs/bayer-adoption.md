# Using the conversion engine in an internal data flow

This page describes how to run EZ Mapper's conversion engine (`iuclid_engine`) as a repeatable step in an internal regulatory data flow. It also lists the decisions and checks that are still needed before the engine is used for real submissions.

## Target flow

```
source systems --extract--> standard table (CSV/XLSX)
                              |
                              +- classify  (study-type / guideline code table -> template)
                              +- convert   (managed mapping + code lists + link registry)
                              +- validate  (picklists, required fields, IUCLID XSDs)
                              v
                       .i6z + run report + audit log entry
                              |
                              v
               import into IUCLID (manual import; API push not implemented)
```

1. **Extract** each source system into one table per template, with one row per study record. Extraction depends on the source system and is not part of this repository.
2. **Classify** rows when a table mixes study types:
   `python -m iuclid_engine classify --input all.csv --code-column guideline --table pipeline_config/classification/oecd_guideline_to_oht.csv --split-dir split/`
   Codes that are not in the table get no template unless `--keyword-fallback` is given. With that option, the app's original keyword rules are used for them. Replace the example table with the internal study-type codes.
3. **Convert**:
   `python -m iuclid_engine convert --input split/<template>.csv --mapping pipeline_config/mappings/<source>/<template>.json --links pipeline_config/links/<registry>.json --xsd-dir <xsds> --settings pipeline_config/format_settings.json --output out/<run>.i6z --strict`
4. **Review** `out/<run>.report.json`. Rejected picklist values, missing required fields and schema errors count as errors and make the command exit with code 2. With `--strict`, no `.i6z` is written in that case.
5. **Import** the `.i6z` into IUCLID.

The same steps are available from Python through `iuclid_engine.pipeline.convert(...)`, which returns the report as a dictionary.

## Configuration under version control (`pipeline_config/`)

| Item | Example | Purpose |
| --- | --- | --- |
| Managed mapping | `mappings/toxval/oht67_repeated_dose_oral.json` | One file per source system and template. It holds `mapping_id`, `version`, `source_system`, `template`, `reviewed_by`, `reviewed_on`, the column-to-IUCLID-path mapping, constants, required fields and the code lists to apply. Flat mappings downloaded from the app (`{column: path}`) are also accepted. |
| Code list | `codelists/example_source_vocabulary.json` | Translates source vocabulary into IUCLID phrase labels or codes. Terms can be keyed by full field path, by the last path segment (e.g. `species`) or by `*` for all picklists. |
| Classification table | `classification/oecd_guideline_to_oht.csv` | `code,oht_class` rows. `oht_class` is the template label shown in the app (e.g. `OHT 67: Repeated Dose Toxicity Oral`). |
| Link registry | `links/example_link_registry.json` | Existing IUCLID document keys per entity, looked up by the value of a mapped field (e.g. the substance name). Matching rows link to the existing document instead of creating a new one, so repeated imports don't create duplicate substances or legal entities. |
| Format settings | `format_settings.example.json` | IUCLID version, definition, namespace and legislation versions written to the `.i6z` metadata. Set them to the values of the internal IUCLID installation. |

Every run report and audit entry records:
- the SHA-256 and `version` of the mapping and code lists
- the input and output checksums
- the operator (`--operator`, `IUCLID_ENGINE_OPERATOR` or the login name)
- the tool version (`IUCLID_ENGINE_BUILD`, otherwise the git commit)

Keep the audit log on append-only storage.

### Picklist values

The engine resolves a mapped value for a picklist field in this order:

1. An IUCLID phrase code that is valid for the field.
2. A code-list translation.
3. An IUCLID phrase label from the phrase catalogue (`data/IUCLID6_6_all_fields_update01.xlsx`, sheet `I6Picklists`).
4. The "other:" code (1342) with the value as free text, if the field allows "other".
5. If none of these apply, the value is rejected and listed in the report.

The phrase catalogue in the repository is from IUCLID 6.6. The engine always checks resolved codes against the code lists in `entity_models`, so an outdated catalogue leads to rejected values rather than invalid codes. Replace it with the field definition workbook of the IUCLID version in use.

## Running in a container

```bash
docker build --target engine --build-arg IUCLID_ENGINE_BUILD=$(git rev-parse HEAD) -t iuclid-engine .
docker run --rm -v "$PWD/work:/work" -e IUCLID_ENGINE_OPERATOR=<service account> iuclid-engine \
    convert --input /work/in.csv --mapping /work/mapping.json --output /work/out.i6z
```

The engine image contains no Streamlit or NLP dependencies and runs as a non-root user. The default build target is still the Streamlit app. Because the data is confidential, run both only on internal infrastructure. The GitHub workflow in `.github/workflows` builds the app image and pushes it to GitHub Container Registry; disable or redirect it in an internal fork.

## Open decisions and checks

The code does not settle the following items. They need internal information or access:

- [ ] **Scope**: confirm the IUCLID 6 version and format in use, and whether the output goes to ECHA (REACH) or EFSA (plant protection products). Set `pipeline_config/format_settings.json` to match.
- [ ] **Templates**: list the source systems (study management, lab systems, tox/ecotox databases, CRO reports) and the templates each one feeds. For plant protection products this includes ecotox, environmental fate, residues and metabolism templates. Run `python -m iuclid_engine templates` to see which templates `entity_models` supports.
- [ ] **Entity models**: if the internal IUCLID format differs from the one in `entity_models` (format 9.0, documents `*_8.0`), regenerate the models from that version's format XSDs with xsdata (`xsdata generate <xsd dir> --package <package>`). Keep the existing package layout. `magnituderesidinprocessedcommod_6_8` cannot be loaded as it is, because its package and class names don't match, so the engine skips it.
- [ ] **Output**: decide between importing files and pushing to the IUCLID server's API. Only `.i6z` file output is implemented. Before building a push step, check which import and API functions the internal IUCLID version provides.
- [ ] **Non-production import test**: import generated files into a non-production IUCLID instance and compare the records with the source data. Check in particular the document keys, the parent substance and the links reused from the link registry. None of these have been tested against a live IUCLID.
- [ ] **Schema validation**: get the format XSDs for the internal IUCLID version and run with `--xsd-dir`.
- [ ] **Mappings and code lists**: build and review one mapping per source system and template, and fill in `reviewed_by` and `reviewed_on`. Replace the example code list and classification table with internal vocabulary.
- [ ] **Machine suggestions** (`main.py`, `src/`, `configs/`, `data/`): only keep them if new source formats come up often. They need configuration and reference data for internal fields, and they pull in the heaviest dependencies (spaCy, scispaCy, NLTK). The engine does not depend on them.
- [ ] **Licence**: the repository has no LICENSE file. Check the terms of use and EPA attribution requirements with Legal before adopting the code internally.
