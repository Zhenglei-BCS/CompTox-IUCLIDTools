import io
import json
from pathlib import Path
from typing import Dict
import os
import webbrowser

import mammoth
import pandas as pd
import streamlit as st

from iuclid_engine.classify import classify_by_keywords
from iuclid_engine.i6z import build_i6z
from iuclid_engine.picklists import DEFAULT_PHRASE_CATALOGUE, PhraseCatalogue, PicklistResolver
# Re-exported for ezmapper.py, which uses `from ez_utils import *`.
from iuclid_engine.mapping import apply_column_mapping
from iuclid_engine.models import (get_model_fields, get_oht_classes, load_and_introspect_models,
                                  template_from_oht_label)


def split_camel_case(name):
    """
    Split camel case strings into space-separated words.
    Args:
        name (str): The camel case string to split.
    Returns:
        str: The space-separated string.
    """
    parts = []
    current_word = name[0]

    for char in name[1:]:
        if char.isupper() and current_word[-1].islower():
            parts.append(current_word)
            current_word = char
        else:
            current_word += char

    parts.append(current_word)
    return " ".join(parts)


def format_oht_key(file_stem):
    """
    Create a formatted key from the file name.
    Args:
        file_stem (str): The file name stem.
    Returns:
        str: The formatted OHT key, or None if the file doesn't match the expected pattern.
    """
    if "ENDPOINT_STUDY_RECORD" in file_stem:
        prefix, name = file_stem.split("ENDPOINT_STUDY_RECORD")
    elif "FLEXIBLE_RECORD" in file_stem:
        prefix, name = file_stem.split("FLEXIBLE_RECORD")
    else:
        return None  # Skip files that don't match the expected pattern

    oht_number = prefix.strip(" - ")
    name = name.lstrip(".").split("_v")[0].split("-Nano")[0].split("_OHT")[0]
    descriptive_part = split_camel_case(name)
    return f"{oht_number}: {descriptive_part}"


def read_files_from_dir(folder_path: str) -> Dict[str, pd.DataFrame]:
    """
    Read Excel files from a directory and return a dictionary of DataFrames.
    Args:
        folder_path (str): The path to the directory containing the Excel files.
    Returns:
        Dict[str, pd.DataFrame]: A dictionary mapping file stems to their corresponding DataFrames.
    """
    folder = Path(folder_path)
    return {
        file.stem: pd.read_excel(file, sheet_name=None)
        for file in folder.glob("*.xlsx")
    }


def get_unique_column_names(excel_files: Dict[str, pd.DataFrame]) -> set:
    """
    Get unique column names from a dictionary of DataFrames.
    Args:
        excel_files (Dict[str, pd.DataFrame]): A dictionary mapping file stems to their corresponding DataFrames.
    Returns:
        set: A set of unique column names.
    """
    return {
        col for df in excel_files() for col in df[list(df.keys())[0]].columns
    }


@st.cache_data
def load_oht_files(directory_path: Path) -> dict:
    """
    Load OHT files from the specified directory path.
    Args:
        directory_path (Path): The directory path containing the OHT files.
    Returns:
        dict: A dictionary mapping OHT keys to their corresponding file paths.
    """
    oht_files = {
        format_oht_key(filename.stem): {
            "docx": str(directory_path / f"{filename.stem}.docx")
        }
        for filename in directory_path.iterdir()
        if filename.is_file() and filename.stem.startswith("OHT")
    }
    return oht_files


def initialize_session_state() -> None:
    """
    Initialize session state variables.
    """
    if "split_done" not in st.session_state:
        st.session_state.split_done = False
    if "classify_pressed" not in st.session_state:
        st.session_state.classify_pressed = False
    if "show_data_preview" not in st.session_state:
        st.session_state.show_data_preview = True


def upload_file_to_df(uploaded_file: io.BytesIO) -> pd.DataFrame:
    """
    Turn uploaded file into a DataFrame.
    Args:
        uploaded_file (io.BytesIO): The uploaded file object.
    Returns:
        Optional[pd.DataFrame]: The DataFrame created from the uploaded file, or None if no file is uploaded.
    """
    if uploaded_file is not None:
        file_extension = uploaded_file.name.split(".")[-1].upper()
        if file_extension == "CSV":
            user_df = pd.read_csv(uploaded_file)
        elif file_extension == "XLSX":
            user_df = pd.read_excel(uploaded_file)
        else:
            raise ValueError(f"Unsupported file format: {file_extension}")
        return user_df
    else:
        return None


def display_data_preview(user_df: pd.DataFrame) -> None:
    """
    Display a preview of the user's DataFrame if the classify button has not been pressed.
    Args:
        user_df (pd.DataFrame): The user's DataFrame.
    """
    # if not st.session_state.classify_pressed and st.session_state.show_data_preview:
    st.dataframe(
        user_df.head(5),
        hide_index=True,
        )


def classify_data(user_df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify each row based on specific keywords found within the column values and recommend an OHT class accordingly.
    Args:
        user_df (pd.DataFrame): The user's DataFrame.
    Returns:
        pd.DataFrame: The classified DataFrame with OHT class recommendations and reasoning.
    """
    st.session_state.classify_pressed = True
    return classify_by_keywords(user_df)


def display_data_editor(edited_df: pd.DataFrame, oht_files: dict) -> None:
    """
    Display a data editor for the classified DataFrame.
    Args:
        edited_df (pd.DataFrame): The edited DataFrame.
        oht_files (dict): The dictionary of OHT files.
    """

    disabled_columns = [col for col in list(edited_df.columns) if col != 'OHT_Class']
    st.session_state.df1 = st.data_editor(
        edited_df,
        num_rows="dynamic",
        use_container_width=True,
        disabled=disabled_columns,
        column_config={
            "OHT_Class": st.column_config.SelectboxColumn(
                "OHT_Class",
                options=list(oht_files.keys()),
                help = 'Machine Classified OHT Class. Edit using the dropdown menu per record.',
                required=True,
            ),
            "Classification_Reasoning": st.column_config.TextColumn(
                'Classification_Reasoning',
                help = 'The reason the record was machine classified as the selected OHT.'  
                ),
            "Calculated_Study_Duration": st.column_config.TextColumn(
                'Calculated_Study_Duration',
                help = 'Calculated study duration based on reported duration time/units.'  
                ),
                
        },
    hide_index=True,
    )


def split_dataframe(edited_df: pd.DataFrame) -> None:
    """
    Split the edited DataFrame based on the 'OHT_Class' column and store the grouped DataFrames in the session state.
    Args:
        edited_df (pd.DataFrame): The edited DataFrame.
    """
    grouped_dfs = {category: df for category, df in edited_df.groupby("OHT_Class")}
    st.session_state.grouped_dfs = grouped_dfs
    st.success(f"Original data split into: {len(grouped_dfs)} dataframes.")
    for category, df in grouped_dfs.items():
        st.write(f"DataFrame for OHT_Class '{category}' has {len(df)} rows.")
    st.session_state.split_done = True


def merge_columns(modified_df: pd.DataFrame, merge_col1: str, merge_col2: str, merge_delimiter: str, new_merge_col_name: str) -> None:
    """
    Merge two columns in the modified DataFrame and create a new column with the merged values.
    Args:
        modified_df (pd.DataFrame): DataFrame copied from selected_df.
        merge_col1 (str): The name of the first column to merge.
        merge_col2 (str): The name of the second column to merge.
        new_merge_col_name (str): The name of the new column to store the merged values.
    """
    if merge_col1 and merge_col2 and new_merge_col_name:
        modified_df[new_merge_col_name] = (
            modified_df[merge_col1].astype(str)
            + merge_delimiter
            + modified_df[merge_col2].astype(str)
        )
        st.success(
            f"Columns '{merge_col1}' and '{merge_col2}' merged into '{new_merge_col_name}'"
        )
    else:
        st.error("Please select two columns and specify a new column name.")


def split_column(modified_df: pd.DataFrame, column_to_split: str, split_delimiter: str, split_maxsplit: int) -> None:
    """
    Split a column in the modified DataFrame based on a delimiter and create new columns for the split values.
    Args:
        modified_df (pd.DataFrame): DataFrame copied from selected_df.
        column_to_split (str): The name of the column to split.
        split_delimiter (str): The delimiter used to split the column values.
        split_maxsplit (int): The maximum number of splits to perform.
    """
    if column_to_split and split_delimiter:
        splits = modified_df[column_to_split].str.split(
            split_delimiter, n=split_maxsplit, expand=True
        )
        for i, new_col in enumerate(splits.columns):
            modified_df[f"{column_to_split}_split_{i+1}"] = splits[new_col]
        st.success(
            f"Column '{column_to_split}' split into {len(splits.columns)} columns"
        )
    else:
        st.error("Please select a column and specify a delimiter.")

#%%

# %%

def map_columns(modified_df: pd.DataFrame, unique_cols: set, uploaded_mappings: dict = None, field_suggestions: dict = None) -> dict:
    # # If input dataframe empty, return empty list
    # if modified_df.empty:
    #     return list()
    
    columns_to_map = list(modified_df.columns)
    columns_to_map = [col for col in columns_to_map if col not in ['OHT_Class', 'Classification_Reasoning']] 
    # columns_to_map.remove('OHT_Class')
    # columns_to_map.remove('Classification_Reasoning')

    data = [[user_col, "", "", "", ""] for user_col in columns_to_map]

    if uploaded_mappings:
        for row in data:
            user_col = row[0]
            if user_col in uploaded_mappings:
                mapped_oht_column = uploaded_mappings[user_col]
                common_field_path = ".".join(mapped_oht_column.split(".")[2:])
                matching_oht_column = None
                for oht_col in unique_cols:
                    oht_col_path = ".".join(oht_col.split(".")[2:])
                    if oht_col_path == common_field_path:
                        matching_oht_column = oht_col
                        break
                row[1] = matching_oht_column if matching_oht_column else None
                    #(uploaded_mappings)[user_col]
            else:
                row[1] = None

    if field_suggestions:
        for row in data:
            user_col = row[0]
            if user_col in field_suggestions:
                suggested_col = field_suggestions[user_col]
                row[2] = suggested_col
            else:
                row[2] = None

    df = pd.DataFrame(data, columns=["User Column", "OHT Column", "Machine Suggested Column Mapping", "Expected Value Type", "Picklist Values"])
    opt = ['', 'Picklist', 'Free Text']

    st.divider() # Horizontal divider
    st.title("Step 4: Map Columns to OHT Columns")
    st.write("**Map your columns (optionally Upload Column Mapping file):**")
    column_config = {
        "User Column": st.column_config.Column("User Column", 
                                               width='large',
                                               help='Original input column name'),
        "OHT Column": st.column_config.SelectboxColumn(
            "OHT Column",
            width="large",
            help='Select an OHT Column',
            options=list(unique_cols),
            required=False,
        ),
        "Machine Suggested Column Mapping": st.column_config.Column("Machine Suggested Column to assist with OHT Column selection", 
                                                                    width='large',
                                                                    help='Select a Machine Suggested OHT Column'),
        "Expected Value Type": st.column_config.SelectboxColumn(
            "Expected Value Type",
            width="medium",
            help='Select the expected value type for the column, if applicable',
            options=opt,
            required=False
        ),
    }
    updated_df = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        column_config=column_config,
        key="column_mapping_data_editor",
    )

    if updated_df is not None:
        column_mapping = {}
        used_unique_cols = set()
        for index, row in updated_df.iterrows():
            user_col = row["User Column"]
            unique_col = row["OHT Column"]

            if unique_col and unique_col not in used_unique_cols:
                column_mapping[user_col] = unique_col
                used_unique_cols.add(unique_col)
            elif unique_col and unique_col in used_unique_cols:
                st.error(f"The OHT column '{unique_col}' has already been mapped. Please select another.")

    return column_mapping


def preview_column_mapping(column_mapping: dict, modified_df: pd.DataFrame) -> None:
    """
    Preview the column mapping and provide options to download the mapping and modified data.
    Args:
        column_mapping (dict): The dictionary representing the column mapping.
        modified_df (pd.DataFrame): DataFrame copied from selected_df.
    """
    mapping_json = json.dumps(column_mapping, indent=4)
    st.text_area("Column Mapping JSON:", mapping_json, height=250)
    st.download_button(
        label="Download Column Mapping",
        data=mapping_json,
        file_name="column_mapping.json",
        mime="application/json",
        icon=":material/download:"
    )
    st.download_button(
        label="Download Modified Data",
        data=modified_df.to_csv(index=False).encode("utf-8"),
        file_name="modified_data.csv",
        mime="text/csv",
        icon=":material/download:"
    )


def display_word_document(oht_docx_path: str) -> None:
    """
    Display the Word document for the selected OHT.
    Args:
        oht_docx_path (str): The path to the Word document.
    """
    # https://stackabuse.com/how-to-convert-docx-to-html-with-python-mammoth/
    # Map Docx styles to HTML
    custom_styles = """ 
                    b => b.strong
                    u => u.initialism
                    p[style-name='Heading 1'] => h1.card
                    table => table.table.table-hover
                    """
    # Custom CSS, add HTML tag for tab name default
    docx_css = '''
    <title>OHT Documentation</title>
<style>
td, tr, th {
    border: solid 2px lightgrey;
}

</style>
<table style="border: 5px solid #990000; border-collapse: collapse">
    '''
    # https://getbootstrap.com/docs/4.4/getting-started/introduction/
    bootstrap_css = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/css/bootstrap.min.css" integrity="sha384-Vkoo8x4CGsO3+Hhxv8T/Q5PaXtkKtu6ug5TOeNV6gBiFeWPGFN9MuhOf23Q9Ifjh" crossorigin="anonymous">'
    bootstrap_js = '''
    <script src="https://code.jquery.com/jquery-3.4.1.slim.min.js" integrity="sha384-J6qa4849blE2+poT4WnyKhv5vZF5SrPo0iEjwBvKU7imGFAV0wwj1yYfoRSJoZ+n" crossorigin="anonymous"></script>
    <script src="https://cdn.jsdelivr.net/npm/popper.js@1.16.0/dist/umd/popper.min.js" integrity="sha384-Q6E9RHvbIyZFJoft+2mJbHaEWldlvI9IOYy5n3zV9zzTtmI3UksdQRVvoxMfooAo" crossorigin="anonymous"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/js/bootstrap.min.js" integrity="sha384-wfSDF2E50Y2D1uUdj0O3uMBJnjuUD4Ih7YwaYd1iqfktj0Uod8GCExl3Og8ifwB6" crossorigin="anonymous"></script>
    ''' 

    # with st.expander("Click to show WORD document for the OHT", icon=":material/description:"):
    with open(oht_docx_path, 'rb') as doc:
        result_html = mammoth.convert_to_html(doc, style_map = custom_styles)
        html_content = result_html.value
        edited_html = docx_css + bootstrap_css + html_content + bootstrap_js
        # st.components.v1.html(edited_html, height=600, scrolling=True)
        # TODO Discuss how to open from a temporary file between user sessions
        # Write HTML to file and open in new tab
        with open("output/oht_file.html", "w") as f:
            f.write(edited_html)

        # Open the file in a new tab
        webbrowser.open_new_tab(os.path.abspath("output/oht_file.html"))


@st.cache_resource
def load_picklist_resolver() -> PicklistResolver:
    """Resolver that turns IUCLID phrase labels (e.g. "rat") into phrase codes."""
    catalogue = PhraseCatalogue.load(DEFAULT_PHRASE_CATALOGUE) if DEFAULT_PHRASE_CATALOGUE.exists() else None
    return PicklistResolver(catalogue=catalogue)


def generate_i6z(data, other_files=None, resolver=None, links=None, parent_substance_key=None, settings=None):
    """Build the i6z for the mapped data into a fresh buffer stored in the session state.

    Returns the engine BuildResult so the app can show rejected values and other issues.
    """
    resolver = resolver or load_picklist_resolver()
    st.session_state['i6z_io_buffer'] = io.BytesIO()
    return build_i6z(data, st.session_state['i6z_io_buffer'], settings=settings, resolver=resolver, links=links,
                     parent_substance_key=parent_substance_key, attachments=other_files)
