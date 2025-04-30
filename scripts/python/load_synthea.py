import json
import pandas as pd
import os
import glob
import importlib
from collections import defaultdict
from fhirclient.models import fhirabstractresource
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def parse_fhir_bundles(dir_path):
    all_resources = defaultdict(list)
    json_files = glob.glob(os.path.join(dir_path, '*.json'))

    if not json_files:
        logging.warning(f"No .json files found in directory: {dir_path}")
        return {}

    logging.info(f"Found {len(json_files)} JSON files to process in '{dir_path}'.")

    processed_files_count = 0
    error_files_count = 0

    # Iterate through each JSON file
    for file_path in json_files:
        file_basename = os.path.basename(file_path)
        logging.info(f"Processing file: {file_basename}...")
        try:
            # Open and load the JSON data
            with open(file_path, 'r', encoding='utf-8') as f:
                bundle_json = json.load(f)

            # Validate if the JSON is a FHIR Bundle
            if not isinstance(bundle_json, dict) or bundle_json.get("resourceType") != "Bundle":
                logging.warning(f"Skipping {file_basename} - Not a FHIR Bundle or invalid format.")
                continue

            # Process resources within the Bundle's 'entry' array
            if 'entry' in bundle_json:
                for entry in bundle_json.get('entry', []):
                    # Ensure the entry has a 'resource' key
                    if 'resource' in entry and isinstance(entry['resource'], dict):
                        resource_json = entry['resource']
                        resource_type = resource_json.get("resourceType")

                        # Ensure the resource has a 'resourceType'
                        if not resource_type:
                            logging.warning(f"Skipping resource in {file_basename} - Missing resourceType.")
                            continue

                        # Dynamically Create FHIR Object
                        try:
                            # 1. Determine the module name (lowercase resource type)
                            module_name = resource_type.lower()
                            # 2. Dynamically import the corresponding fhirclient module
                            model_module = importlib.import_module(f"fhirclient.models.{module_name}")
                            # 3. Get the actual class definition from the imported module
                            ModelClass = getattr(model_module, resource_type)
                            # 4. Instantiate the FHIR object using the JSON data
                            fhir_object = ModelClass(jsondict=resource_json)

                            # Add the created object to the collection
                            all_resources[resource_type].append(fhir_object)

                        # Handle model creation errors
                        except ModuleNotFoundError:
                             logging.warning(f"Model module not found for resource type '{resource_type}'. Skipping resource.")
                        except AttributeError:
                             logging.warning(f"Class '{resource_type}' not found in module '{module_name}'. Skipping resource.")
                        except Exception as e_resource:
                            logging.error(f"Error creating FHIR object for resource type {resource_type} in {file_basename}: {e_resource}")
                    else:
                         logging.warning(f"Skipping entry in {file_basename} - Missing or invalid 'resource' key.")


            processed_files_count += 1

        # Handle file-level errors
        except FileNotFoundError:
            logging.error(f"File not found - {file_path}")
            error_files_count += 1
        except json.JSONDecodeError:
            logging.error(f"Could not decode JSON from file - {file_basename}")
            error_files_count += 1
        except Exception as e:
            logging.error(f"An unexpected error occurred processing {file_basename}: {e}")
            error_files_count += 1

    # Log summary statistics
    logging.info(f"\nFinished processing {processed_files_count} files ({error_files_count} errors).")
    if all_resources:
        logging.info(f"Found resources: { {k: len(v) for k, v in all_resources.items()} }")
    else:
        logging.info("No FHIR resources were successfully parsed.")

    # Return the dictionary (even if empty)
    return dict(all_resources)


def fhir_objects_to_dataframes(fhir_objects_dict):
    dataframes = {}

    # Iterate through each resource type and its list of objects
    for resource_type, objects_list in fhir_objects_dict.items():
        logging.info(f"\nConverting {len(objects_list)} '{resource_type}' resources to DataFrame...")

        # Skip if no objects exist for this type
        if not objects_list:
            logging.info(f"Skipping '{resource_type}' - No objects found.")
            continue

        try:
            # 1. Convert FHIR objects back to JSON dictionaries
            json_list = []
            for i, obj in enumerate(objects_list):
                try:
                    json_list.append(obj.as_json())
                except AttributeError:
                     logging.warning(f"Object {i} of type '{resource_type}' does not have 'as_json' method. Skipping object.")
                except Exception as json_err:
                     logging.warning(f"Error calling 'as_json' on object {i} of type '{resource_type}': {json_err}. Skipping object.")

            if not json_list:
                 logging.info(f"Skipping DataFrame creation for '{resource_type}' - No valid JSON representations found.")
                 continue

            # 2. Start with max_level=1 to handle top-level nesting first and make list identification more reliable.
            df = pd.json_normalize(json_list, errors='ignore', max_level=1, sep='_')
            logging.info(f"  Initial normalization shape for '{resource_type}': {df.shape}")

            # 3. Keep iterating as long as exploding/normalizing changes the structure
            processed_cols = set()
            while True:
                made_change = False
                current_cols = df.columns.tolist()

                cols_to_explode_this_pass = []
                for col in current_cols:
                    if col in processed_cols: continue

                    series = df[col].dropna()
                    if not series.empty and series.apply(isinstance, args=(list,)).all():
                        try:
                            first_list = series.iloc[0]
                            if first_list and isinstance(first_list[0], dict):
                                cols_to_explode_this_pass.append(col)
                        except IndexError:
                             # Handle cases where the list might be empty
                             pass


                if cols_to_explode_this_pass:
                    logging.info(f"  - Found list columns to explode: {cols_to_explode_this_pass}")
                    for col in cols_to_explode_this_pass:
                        logging.info(f"    - Exploding column '{col}'...") # Format to make output more readable
                        try:
                            # Explode the column containing lists
                            df = df.explode(col, ignore_index=True)
                            logging.info(f"      - Exploded '{col}'. New shape: {df.shape}")

                            # Normalize the column if it now contains dicts
                            series_after_explode = df[col].dropna()
                            # Check if series is not empty and first element is a dict
                            if not series_after_explode.empty and isinstance(series_after_explode.iloc[0], dict):
                                logging.info(f"      - Normalizing dictionary contents of '{col}' post-explosion...")
                                try:
                                    # Normalize the dictionaries from the exploded column
                                    normalized_col_df = pd.json_normalize(df[col].tolist(), errors='ignore', sep='_')
                                    # Prefix new column names to avoid clashes
                                    normalized_col_df.columns = [f"{col}_{sub_col}" for sub_col in normalized_col_df.columns]

                                    # Reset index on both DataFrames for accurate joining
                                    df = df.reset_index(drop=True)
                                    normalized_col_df = normalized_col_df.reset_index(drop=True)

                                    # Drop the original complex column and join the new normalized ones
                                    df = df.drop(columns=[col])
                                    df = df.join(normalized_col_df)
                                    logging.info(f"      - Joined normalized sub-columns from '{col}'. New shape: {df.shape}")
                                    made_change = True # Mark that structure changed
                                except Exception as norm_err:
                                    logging.error(f"      - Error normalizing column '{col}' after explode: {norm_err}")
                            processed_cols.add(col)
                        except Exception as explode_err:
                            logging.error(f"    - Error exploding column '{col}': {explode_err}")
                            processed_cols.add(col) # Mark as processed even if error

                # After handling lists, check for any remaining dict columns
                cols_to_normalize_this_pass = []
                current_cols_after_explode = df.columns.tolist() # Update columns list
                for col in current_cols_after_explode:
                     if col in processed_cols: continue

                     series = df[col].dropna()
                     if not series.empty and series.apply(isinstance, args=(dict,)).all():
                          cols_to_normalize_this_pass.append(col)

                if cols_to_normalize_this_pass:
                     logging.info(f"  - Found dictionary columns to normalize: {cols_to_normalize_this_pass}")
                     for col in cols_to_normalize_this_pass:
                          logging.info(f"    - Normalizing dict column '{col}'...")
                          try:
                               normalized_col_df = pd.json_normalize(df[col].tolist(), errors='ignore', sep='_')
                               normalized_col_df.columns = [f"{col}_{sub_col}" for sub_col in normalized_col_df.columns]
                               df = df.reset_index(drop=True)
                               normalized_col_df = normalized_col_df.reset_index(drop=True)
                               df = df.drop(columns=[col])
                               df = df.join(normalized_col_df)
                               logging.info(f"      - Joined normalized sub-columns from '{col}'. New shape: {df.shape}")
                               made_change = True
                          except Exception as final_norm_err:
                               logging.error(f"    - Error normalizing remaining dict column '{col}': {final_norm_err}")
                          processed_cols.add(col)

                # If no changes were made in this pass (no lists exploded, no dicts normalized), exit loop
                if not made_change:
                    break
                else:
                    processed_cols.clear() # Reset for the next pass if changes were made

            dataframes[resource_type] = df
            logging.info(f"  Finished processing '{resource_type}'. Final shape: {df.shape}")

        except Exception as e:
            logging.error(f"Error converting '{resource_type}' to DataFrame using normalization: {e}")

    logging.info("\nFinished converting objects to DataFrames.")
    return dataframes

if __name__ == "__main__":
    fhir_bundle_directory = '/workspaces/dbt-synthea/data/fhir'

    # Check if the directory exists
    if not os.path.isdir(fhir_bundle_directory):
         logging.critical(f"Directory not found - {fhir_bundle_directory}")
         logging.critical("Please update the 'fhir_bundle_directory' variable with the correct path.")
    else:
        # 1. Parse the FHIR bundles into objects
        fhir_data = parse_fhir_bundles(fhir_bundle_directory)

        # 2. Convert the objects into DataFrames
        if fhir_data: # Proceed only if parsing yielded some data
            fhir_dataframes = fhir_objects_to_dataframes(fhir_data)
        else:
             logging.info("\nNo FHIR data was parsed, skipping DataFrame conversion.")
