"""Assign rows to OECD Harmonised Templates (OHTs).

Two strategies are available:

* :func:`classify_by_code` - explicit lookup of a study-type code or test
  guideline column in a reviewed table (preferred for production data);
* :func:`classify_by_keywords` - the original keyword heuristics of EZ Mapper.

Both add ``OHT_Class`` and ``Classification_Reasoning`` columns. Labels use
the ``"OHT <n>: <Name>"`` format of the Streamlit app; use
:func:`iuclid_engine.models.template_from_oht_label` to get the template name.
"""
import csv
import re
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

UNKNOWN = "Unknown"

# Define keyword conditions for OHTs (with OR groups and AND logic)
KEYWORD_CONDITIONS = {
    "OHT 72: Carcinogenicity": {"and": [["cancer", "carcinogenicity"]]},
    "OHT 73: Toxicity Reproduction": {"and": [["reproduction", "reproductive", "generation"]]},
    "OHT 74: Developmental Toxicity Teratogenicity": {"and": [["developmental", "teratogenicity", "gd"]]},
    "OHT 76: Neurotoxicity": {"and": [["neurotoxicity"]]},
    "OHT 41: Short Term Tox to Fish": {"and": [["fish"], ["short-term"]]},
    "OHT 42: Long Term Tox to Fish": {"and": [["fish"]]},
    "OHT 70: Genetic Toxicity Vitro": {"and": [["vitro"]]},
    "OHT 71: Genetic Toxicity Vivo": {"and": [["vivo"]]},
    "OHT 64: Skin Irritation/Corrosion": {"and": [["skin"], ["irritation", "corrosion"]]},
    "OHT 65: Eye Irritation": {"and": [["eye"], ["irritation"]]},
    "OHT 66-1: Skin Sensitisation": {"and": [["skin"], ["sensitization", "sensitisation"]]},
    "OHT 60: Acute Toxicity Oral": {"and": [["acute"], ["oral"]]},
    "OHT 61: Acute Toxicity Inhalation": {"and": [["acute"], ["inhalation"]]},
    "OHT 62: Acute Toxicity Dermal": {"and": [["acute"], ["dermal"]]},
    "OHT 67: Repeated Dose Toxicity Oral": {"and": [["repeated", "short-term", "chronic", "subchronic",
                                                     "sub-chronic"], ["oral"]]},
    "OHT 68: Repeated Dose Toxicity Inhalation": {
        "and": [["repeated", "short-term", "chronic", "subchronic", "sub-chronic"], ["inhalation"]]},
    "OHT 69-1: Repeated Dose Toxicity Dermal": {
        "and": [["repeated", "short-term", "chronic", "subchronic", "sub-chronic"], ["dermal"]]},
    "OHT 63: Acute Toxicity Other Routes": {"and": [["acute"]]},
    "OHT 69-2: Repeated Dose Toxicity Other": {"and": [["repeated", "chronic", "subchronic", "sub-chronic"]]}
}

TIME_CONVERSION = {
    "day": 1,
    "pnd": 1,  # post natal day
    "week": 7,
    "month": 30.4,
    "year": 365.24
}


def _normalise_code(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def load_code_table(path) -> Dict[str, str]:
    """Read a CSV with columns ``code`` and ``oht_class`` (extra columns are ignored)."""
    with open(Path(path), newline="", encoding="utf-8") as f:
        reader = csv.DictReader(row for row in f if not row.lstrip().startswith("#"))
        missing = {"code", "oht_class"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Classification table {path} is missing columns: {sorted(missing)}")
        return {_normalise_code(row["code"]): row["oht_class"].strip() for row in reader if row["code"]}


def classify_by_code(user_df: pd.DataFrame, code_column: str, table: Dict[str, str],
                     fallback_to_keywords: bool = False) -> pd.DataFrame:
    """Classify rows by looking up ``code_column`` in ``table`` (code -> OHT label)."""
    if code_column not in user_df.columns:
        raise KeyError(f"Column {code_column} not found in input")
    classes, reasons = [], []
    for value in user_df[code_column]:
        code = None if pd.isna(value) else _normalise_code(value)
        if code and code in table:
            classes.append(table[code])
            reasons.append(f"{code_column} = '{value}' found in classification table")
        else:
            classes.append(UNKNOWN)
            reasons.append(f"{code_column} = '{value}' not in classification table")
    result = user_df.copy()
    result.insert(0, "Classification_Reasoning", reasons)
    result.insert(0, "OHT_Class", classes)
    if fallback_to_keywords:
        unknown = result["OHT_Class"] == UNKNOWN
        if unknown.any():
            by_keywords = classify_by_keywords(user_df[unknown.values].copy())
            result.loc[unknown, "OHT_Class"] = by_keywords["OHT_Class"].values
            result.loc[unknown, "Classification_Reasoning"] = (
                    result.loc[unknown, "Classification_Reasoning"] + "; keywords: " +
                    by_keywords["Classification_Reasoning"].values)
    return result


def classify_by_keywords(user_df: pd.DataFrame, keyword_conditions: Optional[dict] = None) -> pd.DataFrame:
    """
    Classify each row based on specific keywords found within the column values and recommend an OHT class accordingly.
    Args:
        user_df (pd.DataFrame): The user's DataFrame.
    Returns:
        pd.DataFrame: The classified DataFrame with OHT class recommendations and reasoning.
    """
    keyword_conditions = keyword_conditions or KEYWORD_CONDITIONS
    oht_class_list = []
    classification_reasoning_list = []
    calculated_duration_list = []

    for index, row in user_df.iterrows():
        classification_reasoning = []
        oht_class = None

        # Search for time duration to add keywords
        long_string = " ".join([str(value) for value in row.values]).lower()
        duration_pattern = r"\b(?:\d+\.\d+|\d+)[\s-]*(?:day|pnd|week|month|year)"  # number and time unit
        max_time = 0
        for match in re.findall(duration_pattern, long_string):
            time, unit = re.split(r"[ \-]+", match)  # split by space or hyphen
            time_in_days = float(time) * TIME_CONVERSION[unit]  # convert into days
            if time_in_days > max_time:
                max_time = time_in_days

        if max_time == 0:  # use toxval definitions to define duration
            study_duration = None
        elif max_time < 31:
            study_duration = "short-term"
        elif max_time <= 90:
            study_duration = "subchronic"
        elif max_time > 90:
            study_duration = "chronic"
        else:
            study_duration = "not found"

        calculated_duration_list.append(study_duration)

        # To track keywords found across columns for each OHT
        found_keywords = {oht: [] for oht in keyword_conditions}

        # Loop over each column and check for keywords within the column value
        for col_name, value in row.items():
            if pd.isna(value):
                continue  # Skip if the value is NaN

            value = str(value).lower()  # Convert to lowercase for case-insensitive matching
            tokens = re.split(r'(?<!\w)-|[^a-zA-Z0-9-]+', value)  # Split by non-alphanumeric (not "-")

            # Check for keywords and track which are found in which columns
            for oht, condition in keyword_conditions.items():
                for group in condition["and"]:
                    # If any of the OR keywords from the group are in the tokens, we mark them as found
                    matched_keyword = next((keyword for keyword in group if keyword in tokens), None)
                    if matched_keyword:
                        found_keywords[oht].append((matched_keyword, col_name))

        # Determine the OHT class based on the found keywords
        for oht, condition in keyword_conditions.items():
            required_groups = condition["and"]
            found_groups = [group for group, col in found_keywords[oht]]

            # Check if all required groups have at least one keyword found in any of them
            if all(any(set(group).intersection([found_group]) for found_group in found_groups) for group in
                   required_groups):
                oht_class = oht
                classification_reasoning += [
                    f"Found '{group}' in column '{col}'" for group, col in found_keywords[oht]
                ]
                break  # Stop once the best match is found

        # If no match is found, assign a default "Unknown" OHT class
        if not oht_class:
            oht_class = UNKNOWN
            classification_reasoning.append("No matching criteria found in any columns")

        oht_class_list.append(oht_class)
        classification_reasoning_list.append("; ".join(classification_reasoning))

    # Add the classification columns to the DataFrame
    user_df.insert(0, "Calculated_Study_Duration", calculated_duration_list)
    user_df.insert(0, "Classification_Reasoning", classification_reasoning_list)
    user_df.insert(0, "OHT_Class", oht_class_list)

    return user_df
