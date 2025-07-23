# main_script.py
# -*- coding: utf-8 -*-

import argparse
import configparser
import fnmatch
import importlib.util
import logging
import os
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd

# Gracefully import optional dependencies
try:
    import openpyxl
except ImportError:
    pass

# Gracefully import required dependencies and provide clear error messages
try:
    import pymysql
    from pymysql.err import ProgrammingError
except ImportError:
    print("FATAL ERROR: 'pymysql' library not found. Please install it using 'pip install pymysql'")
    exit(1)

try:
    import psutil
except ImportError:
    print("FATAL ERROR: 'psutil' library not found. Please install it using 'pip install psutil'")
    exit(1)

# Handle different locations of DatabaseError in older/newer pandas
try:
    from pandas.errors import DatabaseError as PandasDatabaseError
except ImportError:
    from pandas.io.sql import DatabaseError as PandasDatabaseError


# ==============================================================================
# --- Configuration & Custom Exceptions ---
# ==============================================================================
ENTITY_CONFIG_MAP = {
    'ks': {
        'zones': ['dev', 'dev1', 'dev3', 'dev4', 'dev7', 'dev9', 'prd', 'sit', 'sit1', 'sit9', 'uat', 'uat1', 'uat9'],
        'env_file': "/nfs/msa/dapscripts/ks/fwk/prd/config/env_config"
    },
    'ksfr': {
        'zones': ['dev', 'dev5', 'dev6', 'prd', 'sit', 'uat', 'uat2'],
        'env_file': "/nfs/msa/dapscripts/ksfr/fwk/prd/config/env_config"
    },
    'ka': {
        'zones': ['dev1', 'dev', 'prd', 'sit', 'uat'],
        'env_file': "/nfs/msa/dapscripts/ka/fwk/prd/config/env_config"
    }
}

class TableNotFoundError(Exception):
    """Custom exception raised when a specific table is not found in the database."""
    pass


# ==============================================================================
# --- Performance Monitoring Utility ---
# ==============================================================================
class ResourceMonitor:
    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self.pid = os.getpid()
        self.process = psutil.Process(self.pid)
        self._running = False
        self._thread = None
        self.start_time = 0.0
        self.end_time = 0.0
        self.cpu_samples, self.mem_samples_mb, self.disk_read_samples_mb_s, self.disk_write_samples_mb_s = [], [], [], []
        self.summary = {}

    def _monitor_loop(self):
        try:
            last_io = self.process.io_counters()
            last_time = time.time()
            self.process.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self._running = False
            return

        while self._running:
            try:
                self.cpu_samples.append(self.process.cpu_percent(interval=None))
                self.mem_samples_mb.append(self.process.memory_info().rss / (1024 * 1024))
                current_time = time.time()
                current_io = self.process.io_counters()
                time_delta = current_time - last_time
                if time_delta > 0:
                    read_rate = (current_io.read_bytes - last_io.read_bytes) / time_delta / (1024*1024)
                    write_rate = (current_io.write_bytes - last_io.write_bytes) / time_delta / (1024*1024)
                    self.disk_read_samples_mb_s.append(read_rate)
                    self.disk_write_samples_mb_s.append(write_rate)
                last_io = current_io
                last_time = current_time
                time.sleep(self.interval)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._running = False
                break

    def __enter__(self):
        self.start_time = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logging.info("Resource monitor started.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.interval * 2)
        logging.info("Resource monitor stopped.")
        self._calculate_summary()

    def _calculate_summary(self):
        duration = self.end_time - self.start_time
        def _get_peak(samples):
            if not samples: return (0, 0)
            peak_val = max(samples)
            peak_time_index = samples.index(peak_val)
            peak_time_s = peak_time_index * self.interval
            return (peak_val, peak_time_s)
        def _get_avg(samples):
            return sum(samples) / len(samples) if samples else 0
        self.summary = {
            'duration_s': duration, 'peak_cpu_percent': _get_peak(self.cpu_samples),
            'avg_cpu_percent': _get_avg(self.cpu_samples), 'peak_mem_mb': _get_peak(self.mem_samples_mb),
            'avg_mem_mb': _get_avg(self.mem_samples_mb), 'peak_disk_read_mbs': _get_peak(self.disk_read_samples_mb_s),
            'avg_disk_read_mbs': _get_avg(self.disk_read_samples_mb_s),
            'peak_disk_write_mbs': _get_peak(self.disk_write_samples_mb_s),
            'avg_disk_write_mbs': _get_avg(self.disk_write_samples_mb_s)
        }

    def get_formatted_summary(self) -> str:
        if not self.summary: self._calculate_summary()
        s = self.summary
        return (f"✅ STATUS: SUCCESS\n--- 📊 Performance Summary ---\n"
                f"Execution Duration: {s['duration_s']:.1f}s\n"
                f"Peak CPU Usage: {s['peak_cpu_percent'][0]:.2f} % (at ~{s['peak_cpu_percent'][1]:.1f}s)\n"
                f"Avg. CPU Usage: {s['avg_cpu_percent']:.2f} %\n"
                f"Peak Memory Usage: {s['peak_mem_mb'][0]:.2f} MB (at ~{s['peak_mem_mb'][1]:.1f}s)\n"
                f"Avg. Memory Usage: {s['avg_mem_mb']:.2f} MB\n"
                f"Peak Disk Read Rate: {s['peak_disk_read_mbs'][0]:.2f} MB/s (at ~{s['peak_disk_read_mbs'][1]:.1f}s)\n"
                f"Avg. Disk Read Rate: {s['avg_disk_read_mbs']:.2f} MB/s\n"
                f"Peak Disk Write Rate: {s['peak_disk_write_mbs'][0]:.2f} MB/s (at ~{s['peak_disk_write_mbs'][1]:.1f}s)\n"
                f"Avg. Disk Write Rate: {s['avg_disk_write_mbs']:.2f} MB/s")

# ==============================================================================
# --- Core Logic Classes ---
# ==============================================================================
class CompareFile:
    class FileStatus:
        FOUND_EXACT_MATCH, FOUND_CASE_INSENSITIVE, MISSING_ON_FILESYSTEM, PARENT_DIRECTORY_NOT_FOUND, NO_PATH_IN_DATABASE, EXTRA_ON_FILE_SYSTEM, FILESYSTEM_SCAN_ERROR = 'FOUND_EXACT_MATCH', 'FOUND_CASE_INSENSITIVE', 'MISSING_ON_FILESYSTEM', 'PARENT_DIRECTORY_NOT_FOUND', 'NO_PATH_IN_DATABASE', 'EXTRA_ON_FILE_SYSTEM', 'FILESYSTEM_SCAN_ERROR'

    def __init__(self, entity: str, zone: str, env_file: str, main_env_file: str, template_type: str, ignore_patterns: List[str]):
        self.entity = entity
        self.zone = zone
        self.env_file = env_file
        self.main_env_file = main_env_file
        self.template_type = template_type
        self.logger = logging.getLogger(f"{self.__class__.__name__}.{self.entity}.{self.zone}")
        self.logger.info(f"Initializing for entity '{self.entity}', zone '{self.zone}', type '{self.template_type}'.")
        self.sep = os.sep
        self.env = {}
        self.init_env_config(self.main_env_file)
        self.init_env_config(self.env_file)
        self.fwk_config = self.env["db_config_file"]
        self.database_config = self.get_database_config()
        self.ignore_literals = set()
        self.ignore_patterns_wildcard = []
        for p in ignore_patterns:
            pl = p.lower()
            if any(c in pl for c in '*?['):
                self.ignore_patterns_wildcard.append(pl)
            else:
                self.ignore_literals.add(pl)

    def init_env_config(self, filepath, sep='=', comment_char='#'):
        props = self.env
        try:
            with open(filepath, "rt", encoding='utf-8') as f:
                for line in f:
                    l = line.strip()
                    if l and not l.startswith(comment_char):
                        key_value = l.split(sep)
                        key_parts = key_value[0].strip().split()
                        if len(key_parts) > 1:
                            props[key_parts[1].strip()] = sep.join(key_value[1:]).strip()
            for key, value in props.items():
                tKey = f"${{{key}}}"
                for ikey in props:
                    if ikey != key:
                        props[ikey] = props[ikey].replace(tKey, value)
        except FileNotFoundError:
            self.logger.error(f"Environment config file not found: {filepath}")
            raise

    def get_database_config(self) -> Dict:
        parser = configparser.ConfigParser()
        parser.read(self.fwk_config)
        main_cryptography = self.env["main_cryptography"]
        decryption_script_path = Path(main_cryptography) / "decryption_string.py"
        spec = importlib.util.spec_from_file_location("module.name", str(decryption_script_path))
        Decrypted = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(Decrypted)
        decrpted = Decrypted.Decrypted(key=parser.get("config", "private"), data=parser.get("config", "password"))
        return {
            "user": parser.get("config", "user"), "password": decrpted.decrypt_data(),
            "host": parser.get("config", "host"), "port": int(parser.get("config", "port")),
            "database": parser.get("config", "database"), "autocommit": True, "charset": "utf8"
        }

    def get_database_connection(self):
        return pymysql.connect(**self.database_config, init_command="SET sql_mode = 'PIPES_AS_CONCAT';")

    def execute_query(self, query: str) -> pd.DataFrame:
        try:
            self.logger.info("Connecting to MariaDB...")
            with self.get_database_connection() as conn:
                self.logger.info("Connected to MariaDB.")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    df = pd.read_sql_query(query, conn)
            self.logger.info("Connection automatically closed.")
            if df.empty:
                self.logger.warning("Query executed successfully, but no data was returned.")
            return df
        except PandasDatabaseError as e:
            if e.__cause__ and isinstance(e.__cause__, ProgrammingError) and e.__cause__.args[0] == 1146:
                raise TableNotFoundError(str(e)) from e
            else:
                self.logger.error(f"DATABASE ERROR occurred: {e}", exc_info=False) # No need for full traceback for this
                return pd.DataFrame()
        except Exception as generic_error:
            self.logger.error(f"UNEXPECTED ERROR during query: {generic_error}", exc_info=True)
            return pd.DataFrame()

    def scan_filesystem(self, root_path: str) -> set:
        self.logger.debug(f"Scanning for files in '{root_path}'...")
        if not os.path.isdir(root_path):
            self.logger.warning(f"Scan path '{root_path}' does not exist or is not a directory.")
            return set()
        found_files = set()
        for dirpath, _, filenames in os.walk(root_path):
            for filename in filenames:
                file_lower = filename.lower()
                if file_lower in self.ignore_literals: continue
                if any(file_lower.endswith(ext) for ext in self.ignore_literals if ext.startswith('.')): continue
                if any(fnmatch.fnmatch(file_lower, pattern) for pattern in self.ignore_patterns_wildcard): continue
                found_files.add(Path(os.path.join(dirpath, filename)).as_posix())
        self.logger.debug(f"Scan complete for '{root_path}'. Found {len(found_files)} files.")
        return found_files

    def get_comparison_columns(self) -> List[str]: raise NotImplementedError()

    def _build_full_file_name(self, df: pd.DataFrame, new_col_name: str, path_col: str, file_col: str) -> pd.DataFrame:
        path_series = df[path_col].fillna('')
        file_series = df[file_col].fillna('')
        new_column = pd.Series([None] * len(df), index=df.index, dtype=object)
        valid_mask = (path_series != '') & (file_series != '')
        new_column.loc[valid_mask] = path_series[valid_mask].str.cat(file_series[valid_mask], sep='/')
        return df.assign(**{new_col_name: new_column})

    def _check_file_status(self, db_filepath: str, dir_cache: Dict) -> Tuple[str, Union[str, None]]:
        S = self.FileStatus
        if not db_filepath or pd.isna(db_filepath): return S.NO_PATH_IN_DATABASE, None
        try:
            path_obj = Path(db_filepath)
            directory = path_obj.parent.as_posix()
            filename_to_find = path_obj.name
            if directory in dir_cache:
                filename_lookup = dir_cache[directory]
                filename_lower = filename_to_find.lower()
                if filename_lower in filename_lookup:
                    actual_filename = filename_lookup[filename_lower]
                    found_path = f"{directory}/{actual_filename}"
                    return (S.FOUND_EXACT_MATCH if filename_to_find == actual_filename else S.FOUND_CASE_INSENSITIVE), found_path
            if os.path.exists(db_filepath): return S.FOUND_EXACT_MATCH, db_filepath
            if not os.path.isdir(directory): return S.PARENT_DIRECTORY_NOT_FOUND, None
            return S.MISSING_ON_FILESYSTEM, None
        except Exception as e:
            self.logger.error(f"Error during filesystem check for path '{db_filepath}': {e}", exc_info=True)
            return S.FILESYSTEM_SCAN_ERROR, None

    def prepare_dataframe(self, df_from_db: pd.DataFrame) -> pd.DataFrame: raise NotImplementedError()

    def perform_comparison(self, df_from_db: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("Comparing database records with filesystem...")
        prepared_dataframe = self.prepare_dataframe(df_from_db)
        comparison_cols = self.get_comparison_columns()
        self.logger.info(f"Comparing based on columns: {comparison_cols}")
        if not all(col in prepared_dataframe.columns for col in comparison_cols):
            raise ValueError("Columns from get_comparison_columns not found.")
        id_vars = [col for col in prepared_dataframe.columns if col not in comparison_cols]
        melted_df = prepared_dataframe.melt(id_vars=id_vars, value_vars=comparison_cols, var_name='source_column', value_name='filepath_to_check')
        melted_df.dropna(subset=['filepath_to_check'], inplace=True)
        melted_df = melted_df[melted_df['filepath_to_check'] != '']
        all_db_path_to_check = set(melted_df['filepath_to_check'].dropna())
        all_dirs_from_db = {str(Path(path).parent.as_posix()) for path in all_db_path_to_check}
        self.logger.info(f"Building cache for {len(all_dirs_from_db)} directories...")
        dir_cache = {}
        for directory in all_dirs_from_db:
            if not os.path.isdir(directory): continue
            try:
                dir_cache[directory] = {f.lower(): f for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))}
            except OSError as e:
                self.logger.warning(f"Could not read directory '{directory}': {e}")
        self.logger.info("Directory cache built.")
        self.logger.info("Checking file status for all records...")
        melted_df[['status', 'actual_fs_path']] = melted_df['filepath_to_check'].apply(lambda path: pd.Series(self._check_file_status(path, dir_cache)))
        all_fs_paths_found = set(melted_df['actual_fs_path'].dropna())
        all_actual_files_in_dirs = set()
        for directory in all_dirs_from_db:
            all_actual_files_in_dirs.update(self.scan_filesystem(directory))
        extra_files_on_fs = all_actual_files_in_dirs - all_fs_paths_found
        if extra_files_on_fs:
            self.logger.info(f"Found {len(extra_files_on_fs)} extra files.")
            extra_files_df = pd.DataFrame({'filepath_to_check': list(extra_files_on_fs), 'status': self.FileStatus.EXTRA_ON_FILE_SYSTEM, 'source_column': 'FILESYSTEM'})
            return pd.concat([melted_df, extra_files_df], ignore_index=True)
        else:
            self.logger.info("No extra files found on the filesystem.")
            return melted_df

    def get_query(self): raise NotImplementedError()

    def run(self) -> Optional[pd.DataFrame]:
        try:
            query = self.get_query()
            if not query:
                self.logger.error("No query provided. Aborting.")
                return None
            df_from_db = self.execute_query(query)
            if df_from_db is None or df_from_db.empty:
                self.logger.warning("Execution finished early as no data was returned from DB or query failed gracefully.")
                return None
            self.logger.info("--- Comparing data ---")
            final_report = self.perform_comparison(df_from_db)
            if not final_report.empty:
                final_report['entity'] = self.entity
                final_report['zone'] = self.zone
            return final_report
        except TableNotFoundError as e:
            self.logger.warning(f"Skipping task for [{self.entity}/{self.zone}] because a required table was not found. Details: {e}")
            return None
        except ValueError as e:
            self.logger.error(f"A validation error occurred: {e}")
            return None
        except Exception as e:
            self.logger.error(f"An unexpected error occurred during run: {e}", exc_info=True)
            return None

class ComparePstCrt(CompareFile):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger.info("--- Initialized for PSTCRT ---")
    def get_query(self) -> str:
        return f"SELECT DISTINCT template_name, script_path, script_name, script_config_file FROM {self.entity}{self.zone}_fwkdb.template_mstr;"
    def prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("Preparing PSTCRT dataframe...")
        df = self._build_full_file_name(df, 'pstcrt_script_full_name', 'script_path', 'script_name')
        df = self._build_full_file_name(df, 'pstcrt_script_config_file', 'script_path', 'script_config_file')
        return df.drop(columns=['script_path', 'script_name', 'script_config_file'])
    def get_comparison_columns(self) -> List[str]:
        return ['pstcrt_script_full_name', 'pstcrt_script_config_file']

class CompareOutbound(CompareFile):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger.info("--- Initialized for OUTBOUND ---")
    def get_query(self) -> str:
        return f"SELECT workflow_nm, obd_hql_path, obd_hql_nm, header_file_nm, footer_file_nm, fix_len_config_file_nm, special_template, special_script_full_path FROM {self.entity}{self.zone}_fwkdb.outbound_file_mstr;"
    def prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("Preparing Outbound dataframe...")
        df = self._build_full_file_name(df, 'obd_script_full_name', 'obd_hql_path', 'obd_hql_nm')
        df = self._build_full_file_name(df, 'obd_header_full_name', 'obd_hql_path', 'header_file_nm')
        df = self._build_full_file_name(df, 'obd_footer_full_name', 'obd_hql_path', 'footer_file_nm')
        df = self._build_full_file_name(df, 'obd_fix_len_full_name', 'obd_hql_path', 'fix_len_config_file_nm')
        special_base_path = df['special_script_full_path'].fillna('')
        special_template_name = df['special_template'].fillna('')
        new_df = df.assign(obd_special_script_ctl_name=None, obd_special_script_dat_name=None)
        valid_mask = (special_base_path != '') & (special_template_name != '')
        new_df.loc[valid_mask, 'obd_special_script_ctl_name'] = special_base_path[valid_mask] + "/convert_ob_ctl_" + special_template_name[valid_mask] + ".sh"
        new_df.loc[valid_mask, 'obd_special_script_dat_name'] = special_base_path[valid_mask] + "/convert_ob_dat_" + special_template_name[valid_mask] + ".sh"
        cols_to_drop = ['obd_hql_path','obd_hql_nm', 'header_file_nm', 'footer_file_nm', 'fix_len_config_file_nm', 'special_script_full_path', 'special_template']
        return new_df.drop(columns=cols_to_drop)
    def get_comparison_columns(self) -> List[str]:
        return ['obd_script_full_name', 'obd_header_full_name', 'obd_footer_full_name', 'obd_fix_len_full_name', 'obd_special_script_ctl_name', 'obd_special_script_dat_name']


# ==============================================================================
# --- Output Writer Functions ---
# ==============================================================================
def sanitize_dataframe_for_writing(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cleans string data in a DataFrame to prevent Unicode errors on writing by replacing invalid characters.
    """
    df_copy = df.copy()
    for col in df_copy.select_dtypes(include=['object']).columns:
        df_copy[col] = df_copy[col].apply(
            lambda x: x.encode('utf-8', 'replace').decode('utf-8') if isinstance(x, str) else x
        )
    return df_copy

def write_to_excel(df: pd.DataFrame, excel_path: str, sheet_name='Compare Report'):
    if not excel_path: return
    if df.empty:
        logging.warning("Skipping Excel write, no data.")
        return
    try:
        import openpyxl
        logging.info("Sanitizing data for Excel export...")
        sanitized_df = sanitize_dataframe_for_writing(df)
        output_path = Path(excel_path)
        if output_path.is_dir():
            default_filename = f"comparison_report_ALL_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            output_path = output_path / default_filename
            logging.warning(f"Output path is a directory. Writing to default file: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        logging.info(f"Writing output to {output_path}...")
        sanitized_df.to_excel(output_path, sheet_name=sheet_name, index=False, engine="openpyxl")
        logging.info("Excel write successful.")
    except ImportError:
        logging.error("Could not write to Excel. 'openpyxl' is required. 'pip install openpyxl'.")
    except Exception as e:
        logging.error(f"Error writing to Excel file {excel_path}: {e}", exc_info=True)

def write_to_csv(df: pd.DataFrame, csv_path: str):
    if not csv_path: return
    if df.empty:
        logging.warning("Skipping CSV write, no data.")
        return
    try:
        logging.info("Sanitizing data for CSV export...")
        sanitized_df = sanitize_dataframe_for_writing(df)
        output_path = Path(csv_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        logging.info(f"Writing output to {output_path}...")
        sanitized_df.to_csv(
            output_path, sep="|", index=False, encoding="utf-8"
        )
        logging.info("CSV write successful.")
    except Exception as e:
        logging.error(f"Error writing to file {csv_path}: {e}")

# ==============================================================================
# --- Main Orchestration Layer ---
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Compares database file paths against the filesystem across multiple entities and zones.")
    parser.add_argument("-m", "--mainEnvFile", required=True)
    parser.add_argument("-e", "--entity", required=False, help="Specify a single entity to run.")
    parser.add_argument("-z", "--zone", required=False, help="Specify a single zone to run (requires -e).")
    parser.add_argument("-t", "--templateType", choices=["PSTCRT", "OUTBOUND", "ALL"], required=True, default="ALL")
    parser.add_argument("-csv", "--csvFile", help="Path to the consolidated output CSV file.")
    parser.add_argument("-excel", "--excelFile", help="Path to the consolidated output Excel file or directory.")
    parser.add_argument("--ignore-ext", nargs='+', default=['*.ds_store', '*.bak*', '*bk*', '*.log*', '*.nfs*', '*.py*', '*.tmp*', '*.temp*'])
    args = parser.parse_args()

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = Path(__file__).parent
    log_dir = script_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_filename = f"comparison_run_{timestamp_str}.log"
    full_log_path = log_dir / log_filename
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S', handlers=[logging.FileHandler(full_log_path, mode='w'), logging.StreamHandler()])
    logging.info("--- SCRIPT START ---")
    logging.info(f"Logging to file: {full_log_path}")
    
    with ResourceMonitor() as monitor:
        tasks_to_process = []
        if args.entity:
            if args.entity not in ENTITY_CONFIG_MAP:
                logging.error(f"Entity '{args.entity}' not found. Aborting.")
                return
            config = ENTITY_CONFIG_MAP[args.entity]
            zones_to_run = [args.zone] if args.zone else config['zones']
            for zone in zones_to_run:
                if zone in config['zones']:
                    tasks_to_process.append({'entity': args.entity, 'zone': zone, 'env_file': config['env_file']})
                else:
                    logging.warning(f"Zone '{zone}' not configured for '{args.entity}'. Skipping.")
        else:
            logging.info("Running in batch mode for ALL configured entities and zones.")
            for entity, config in ENTITY_CONFIG_MAP.items():
                for zone in config['zones']:
                    tasks_to_process.append({'entity': entity, 'zone': zone, 'env_file': config['env_file']})
        
        all_reports = []
        task_map = {"PSTCRT": ComparePstCrt, "OUTBOUND": CompareOutbound}
        for task_params in tasks_to_process:
            logging.info(f"--- Processing: Entity=[{task_params['entity']}], Zone=[{task_params['zone']}] ---")
            common_args = {**task_params, 'main_env_file': args.mainEnvFile, 'template_type': args.templateType, 'ignore_patterns': args.ignore_ext}
            comparison_classes_to_run = list(task_map.values()) if args.templateType == "ALL" else ([task_map[args.templateType]] if args.templateType in task_map else [])
            for CompClass in comparison_classes_to_run:
                try:
                    task_instance = CompClass(**common_args)
                    result_df = task_instance.run()
                    if result_df is not None and not result_df.empty:
                        result_df['task_source'] = CompClass.__name__
                        all_reports.append(result_df)
                except Exception as e:
                    logging.error(f"Failed to execute task {CompClass.__name__} for {task_params}: {e}", exc_info=True)
        
        if all_reports:
            logging.info("--- Consolidating all reports... ---")
            final_df = pd.concat(all_reports, ignore_index=True)
            leading_cols = ['entity', 'zone', 'task_source']
            other_cols = [col for col in final_df.columns if col not in leading_cols]
            final_df = final_df[leading_cols + other_cols]
            logging.info(f"Total records found across all runs: {len(final_df)}")
            write_to_csv(final_df, args.csvFile)
            write_to_excel(final_df, args.excelFile, sheet_name="Consolidated Report")
        else:
            logging.info("No results generated from any tasks.")

    print("-" * 20)
    print(monitor.get_formatted_summary())
    print("-" * 20)
    logging.info("\n" + monitor.get_formatted_summary())
    logging.info("--- SCRIPT END ---")

if __name__ == '__main__':
    main()