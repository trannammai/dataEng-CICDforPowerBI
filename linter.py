"""
This program executes and logs linters for pbip files.
It uses Tabular Editor and PBI Inspector.
Note: Windows only
"""

import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import re
import sys
from distutils.dir_util import copy_tree

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

SUCCESS = True

# Logging Utilities
def log_error(*args, **kwargs):
    global SUCCESS
    SUCCESS = False
    return logging.error(*args, **kwargs)

def log_exception(*args, **kwargs):
    global SUCCESS
    SUCCESS = False
    return logging.exception(*args, **kwargs)

# Decorators
def log_linter(func):
    """
    Decorator that logs the start and end of a linter function,
    as well as the results and score.
    """
    def wrapper(item, *args, **kwargs):
        try:
            linter_results = func(item, *args, **kwargs)
            score = float(linter_results.pop("score"))

            if score >= 8:
                logging.info(f"'{item}' - Score: {score} - Excellent! Details: {linter_results}")
            elif score >= 6:
                logging.warning(f"'{item}' - Score: {score} - Needs attention. Details: {linter_results}")
            else:
                log_error(f"'{item}' - Score: {score} - Poor performance. Details: {linter_results}")
        except Exception as e:
            log_exception(f"{func.__name__} on '{item}' failed with error: {e}")

    return wrapper

# Helpers
def handle_te_output(te_output):
    """Parse JSON from subprocess output"""
    logging.debug(te_output)
    match = re.search(r'\{.*\}', str(te_output.stdout), re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"No JSON object found in the output: {te_output.stdout}")

def handle_pbii_output(test_results, n_visuals):
    """Aggregate and process PBI Inspector results"""
    results = test_results["Results"]
    log_type_mapping = {0: 'error', 1: 'warning'}

    summary = {'errors': 0, 'warnings': 0, 'infos': 0, 'penalty': 0}
    for result in results:
        result_severity = log_type_mapping.get(result["LogType"], 'info')
        result_count = 5 if result["Actual"] is False else len(result["Actual"])

        if result_severity == 'error':
            summary['errors'] += result_count
            summary['penalty'] += result_count * 2
        elif result_severity == 'warning':
            summary['warnings'] += result_count
            summary['penalty'] += result_count
        else:
            summary['infos'] += result_count

    score = max(10 - (summary['penalty'] / n_visuals * 5), 0) if n_visuals else 0
    return {**summary, 'objects': n_visuals, 'score': round(score, 2)}

def get_number_of_visuals(report_root):
    """Count the number of visuals in a report.json file."""
    with open(report_root / 'report.json', 'r', encoding='utf-8') as f:
        report = json.load(f)

    return sum(len(section.get("visualContainers", [])) for section in report.get("sections", []))

# Linter Functions
@log_linter
def model_linter(model_root):
    """Run the Tabular Editor tool on a specified directory."""
    item_path = model_root / 'definition'
    linter_path = Path(__file__).parent / 'TMDLLint'

    args = [
        'dotnet', 'run', '--configuration', 'Release', '--project', linter_path, str(item_path)
    ]
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=120)
    return handle_te_output(result)

@log_linter
def visuals_linter(report_root, rules):
    """Run the PBI Inspector tool on a specified directory."""
    result_dir = tempfile.mkdtemp()
    valid_root = report_root

    if not str(valid_root).islower() or not str(valid_root).endswith('.report'):
        valid_root = Path(tempfile.mkdtemp()) / '.report'
        copy_tree(str(report_root), str(valid_root), verbose=0)

    linter_path = Path(__file__).parent / 'PBI-Inspector' / 'PBIXInspectorCLI'
    command = [
        'dotnet', 'run', '--project', linter_path, '--configuration', 'Release',
        '-pbipreport', str(valid_root), '-output', result_dir,
        '-rules', str(rules), '-formats', 'JSON'
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)
    if 'Error: Could not deserialise rules file with path' in result.stdout:
        raise ValueError(f"Invalid rules file format: '{rules}'")

    result_file = Path(result_dir) / os.listdir(result_dir)[0]
    with open(result_file, 'r', encoding='utf-8-sig') as f:
        test_results = json.load(f)

    n_visuals = get_number_of_visuals(report_root)
    return handle_pbii_output(test_results, n_visuals)

# Orchestration
def get_item_info(path, info='type'):
    """Read metadata information from .platform file."""
    platform_file = path / '.platform'
    if platform_file.exists():
        with open(platform_file, 'r', encoding='utf-8') as f:
            return json.load(f).get('metadata', {}).get(info)
    return None

def list_platform_folders(path, max_depth=3):
    """List folders containing a .platform file up to a given depth."""
    if (path / '.platform').exists():
        return [path]

    if max_depth == 0:
        return []

    subfolders = [
        subfolder
        for item in path.iterdir() if item.is_dir()
        for subfolder in list_platform_folders(item, max_depth - 1)
    ]

    return subfolders

def list_items(path):
    """Group items (Reports and SemanticModels) by their parent directory."""
    item_folders = list_platform_folders(path, max_depth=5)
    holding_folders = {folder.parent for folder in item_folders}

    items_dict = {}
    for folder in holding_folders:
        items_dict[folder] = {
            'Report': [item for item in item_folders if item.parent == folder and get_item_info(item) == 'Report'],
            'SemanticModel': [item for item in item_folders if item.parent == folder and get_item_info(item) == 'SemanticModel']
        }

    return items_dict

def run_linter(path=Path('.'), rules=Path(__file__).parent / 'pbi_inspector_rules.json'):
    """Execute linters for all items in the specified path."""
    items_dict = list_items(path)
    if not items_dict:
        logging.warning(f"No items found at {path}")
        return

    for folder, items in items_dict.items():
        logging.info(f"In '{folder}', reviewing: {items}")

        for model in items['SemanticModel']:
            try:
                model_linter(model)
            except Exception as e:
                log_exception(e)

        for report in items['Report']:
            try:
                visuals_linter(report, rules)
            except Exception as e:
                log_exception(e)

def main():
    """Main function to execute linters."""
    paths = [Path(arg) for arg in sys.argv[1:]] if len(sys.argv) > 1 else [Path('.')]

    for path in paths:
        if not path.exists():
            log_error(f"Path {path} does not exist.")
            continue

        try:
            run_linter(path)
        except Exception as e:
            log_exception(e)

if __name__ == '__main__':
    main()
    sys.exit(0 if SUCCESS else 1)
