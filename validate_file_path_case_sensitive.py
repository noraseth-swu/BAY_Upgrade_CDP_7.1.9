from datetime import date, datetime, timedelta
import os
import argparse
import configparser
import importlib.util
import pymysql
import pandas as pd
import openpyxl
import fnmatch
import logging
import warnings
from pathlib import Path
from typing import List, Tuple, Dict, Union

class CompareFile():
    class FileStatus:
        FOUND_EXACT_MATCH = 'FOUND_EXACT_MATCH'
        FOUND_CASE_INSENSITIVE = 'FOUND_CASE_INSENSITIVE'

        MISSING_ON_FILESYSTEM = 'MISSING_ON_FILESYSTEM'
        PARENT_DIRECTORY_NOT_FOUND = 'PARENT_DIRECTORY_NOT_FOUND'

        NO_PATH_IN_DATABASE = 'NO_PATH_IN_DATABASE'

        EXTRA_ON_FILE_SYSTEM = 'EXTRA_ON_FILE_SYSTEM'
        FILESYSTEM_SCAN_ERROR = 'FILESYSTEM_SCAN_ERROR'

    ALLOWED_ZONES = ['dev', 'dev4', 'uat1', 'uat9', 'prd'] 

    
    def __init__(self, args:argparse.Namespace):

        self.main_env_file = args.mainEnvFile
        self.env_file = args.envFile        
        self.template_type = args.templateType
        self.zone = args.zone
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info(f"Initializing for zone '{self.zone}' and type '{self.template_type}'.")
        self._validate_zone()
        self.sep = os.sep
        self.env = {}
        self.init_env_config(self.main_env_file)
        self.init_env_config(self.env_file)        
        self.fwk_config = self.env["db_config_file"]
        self.database_config = self.get_database_config()

        self.start = datetime.now()
        self.end = datetime.now()
        self.duration = self.end - self.start
        

        self.csv_file = args.csvFile
        self.excel_file = args.excelFile

        self.ignore_patterns = []
        self.ignore_literals = set()

        for pattern in args.ignore_ext:
            pattern_lower = pattern.lower()
            if '*' in pattern_lower or '?' in pattern_lower or '[' in pattern_lower:
                self.ignore_patterns.append(pattern_lower)
            else:
                self.ignore_literals.add(pattern_lower)

        self.logger.info(f"Ignoring literal names/extensions: {self.ignore_literals}")
        self.logger.info(f"Ignoring wildcard patterns: {self.ignore_patterns}")


    def _validate_zone(self):
        if self.zone not in CompareFile.ALLOWED_ZONES:
            error_msg = f"Invalid zone '{self.zone}'. Allowed zones are: {CompareFile.ALLOWED_ZONES}"
            raise ValueError(error_msg)


    def init_env_config(self, filepath, sep='=', comment_char='#'):
        props = self.env
        with open(filepath, "rt") as f:
            for line in f:
                l = line.strip()
                if l and not l.startswith(comment_char):
                    key_value = l.split(sep)
                    key = key_value[0].strip().split(' ')[1].strip()
                    value = sep.join(key_value[1:]).strip() 
                    props[key] = value
        for key in props.keys():
            value = props[key]
            tKey = "${" + key +"}"
            for ikey in props.keys():
                if ikey == key:
                    continue
                props[ikey] = props[ikey].replace(tKey, value)

    def get_database_config(self):
        # connect db
        parser = configparser.ConfigParser()        
        parser.read(self.fwk_config)

        # decryption
        main_cryptography = self.env["main_cryptography"]
        decryption_script_path = f"D:\\git\\BAY_Upgrade_CDP_7.1.9\\config\\decryption_string.py"
        # decryption_script_path = Path(main_cryptography) / "decryption_string.py"
        spec = importlib.util.spec_from_file_location(
            "module.name", decryption_script_path
        )
        Decrypted = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(Decrypted)

        decrpted = Decrypted.Decrypted(
            key=parser.get("config", "private"),
            data=parser.get("config", "password")
        )

        dbConfig = {
            "user" : parser.get("config", "user"),
            "password" : decrpted.decrypt_data(),
            "host" : parser.get("config", "host"),
            "port" : int(parser.get("config", "port")),
            "database" : parser.get("config", "database"),
            "autocommit" : True,
            "charset" : "utf8"
        } 
        return dbConfig
    
    def get_database_connection(self):
        dbConfig = self.database_config
        conn = pymysql.connect(
            user=dbConfig["user"],
            password=dbConfig["password"],
            host=dbConfig["host"],
            port=dbConfig["port"],
            database=dbConfig["database"],
            autocommit=dbConfig["autocommit"],
            charset=dbConfig["charset"],
            init_command="SET sql_mode = 'PIPES_AS_CONCAT';",
            # cursorclass=pymysql.cursors.DictCursor,
        ) 
        return conn 
    
    def execute_query(self, query: str) -> pd.DataFrame:
        try:
            self.logger.info("Connecting to MariaDB...")
            with self.get_database_connection() as conn:
                self.logger.info("Connected to MariaDB...")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    df = pd.read_sql_query(query, conn)

            self.logger.info("Connection automatically closed.")
            if df.empty:
                self.logger.warning("Warning: No data found in the database for the given query.")            
            return df
        
        except pymysql.Error as db_error:
            self.logger.error("DATABASE ERROR occurred", exc_info=True)
            return pd.DataFrame()
    
        except Exception as generic_error:
            self.logger.error(f"UNEXPECTED ERROR occurred", exc_info=True)
            return pd.DataFrame()

    def scan_filesystem(self, root_path: str) -> set:
        self.logger.info(f"Scanning for files in '{root_path}'...")
    
        if not os.path.isdir(root_path):
            self.logger.error(f"Error: Scan path '{root_path}' does not exist or is not a directory.")
            return set()
        
        found_files = set()
        for dirpath, _, filenames in os.walk(root_path):
            for filename in filenames:
                file_lower = filename.lower()
                if file_lower in self.ignore_literals:
                    continue
                if any(file_lower.endswith(ext) for ext in self.ignore_literals if ext.startswith('.')):
                    continue
                is_ignored_by_pattern = False
                for pattern in self.ignore_patterns:
                    if fnmatch.fnmatch(file_lower, pattern):
                        is_ignored_by_pattern = True
                        break
                if is_ignored_by_pattern:
                    continue

                full_path = os.path.join(dirpath, filename)
                found_files.add(Path(full_path).as_posix())
    
        self.logger.info(f"Scan complete. Found {len(found_files)} files.")
        return found_files
    
    def get_comparison_columns(self) -> List[str]:
        raise NotImplementedError("Subclasses must implement this method to provide the comparison columns.")
    
    def _build_full_file_name(self, df: pd.DataFrame, new_col_name: str, path_col: str, file_col: str) -> pd.DataFrame:

        path_series = df[path_col].fillna('')
        file_series = df[file_col].fillna('')

        new_column = pd.Series([None] * len(df), index=df.index)

        valid_mask = (path_series != '') & (file_series != '')

        new_column.loc[valid_mask] = path_series[valid_mask].str.cat(file_series[valid_mask], sep='/')
        return df.assign(**{new_col_name: new_column})
    
    def _check_file_status(self, db_filepath: str, dir_cache: Dict) -> Tuple[str, Union[str , None]]:
        S = self.FileStatus

        if not db_filepath or pd.isna(db_filepath):
            return S.NO_PATH_IN_DATABASE, None
        
        if os.path.exists(db_filepath):
            return S.FOUND_EXACT_MATCH, db_filepath
        
        try:
            path_obj = Path(db_filepath)
            directory = path_obj.parent.as_posix()
            filename_to_find = path_obj.name

            if not os.path.isdir(directory):
                return S.PARENT_DIRECTORY_NOT_FOUND, None
            
            filename_lookup = dir_cache.get(directory, {})

            if filename_to_find.lower() in filename_lookup:
                actual_filename = filename_lookup[filename_to_find.lower()]
                found_path = f"{directory}/{actual_filename}"
                return S.FOUND_CASE_INSENSITIVE, found_path
            
            return S.MISSING_ON_FILESYSTEM, None
            
        except Exception as e:
            self.logger.error(f"Error during filesystem check for path '{db_filepath}': {e}", exc_info=True)
            return S.FILESYSTEM_SCAN_ERROR, None
    
    def prepare_dataframe(self):
        raise NotImplementedError("Subclasses must implement the prepare_dataframe method.")
    
    def perform_comparison(self, df_from_db: pd.DataFrame) -> pd.DataFrame:        
        self.logger.info(f"Comparing database records with filesystem...")       
        
        prepared_dataframe = self.prepare_dataframe(df_from_db)
        comparison_cols = self.get_comparison_columns()
        self.logger.info(f"Comparing based on columns: {comparison_cols}")

        if not all(col in prepared_dataframe.columns for col in comparison_cols):
            self.logger.error("Mismatch between get_comparison_columns and columns in prepared_df. Aborting.")
            raise ValueError("Columns from get_comparison_columns not found in the prepared dataframe.")

        id_vars = [col for col in prepared_dataframe.columns if col not in comparison_cols]

        melted_df = prepared_dataframe.melt(
            id_vars=id_vars,
            value_vars=comparison_cols,
            var_name='source_column',
            value_name='filepath_to_check'
        )

        melted_df.dropna(subset=['filepath_to_check'], inplace=True)
        melted_df = melted_df[melted_df['filepath_to_check'] != '']

        all_db_path_to_check = set(melted_df['filepath_to_check'].dropna())
        all_dirs_from_db = {str(Path(path).parent.as_posix()) for path in all_db_path_to_check}

        self.logger.info(f"Building cache for {len(all_dirs_from_db)} directories...")
        dir_cache = {}
        for directory in all_dirs_from_db:
            if not os.path.isdir(directory):
                continue
            try:
                dir_cache[directory] = {
                    f.lower(): f 
                    for f in os.listdir(directory)
                    if os.path.isfile(os.path.join(directory, f))
                }
            except OSError as e:
                self.logger.warning(f"Could not read directory '{directory}': {e}")

        self.logger.info("Directory cache built.")

        self.logger.info("Checking file status for all records...")
        melted_df[['status', 'actual_fs_path']] = melted_df['filepath_to_check'].apply(
            lambda path: pd.Series(self._check_file_status(path, dir_cache))
        )
        
        all_fs_paths_found = set(melted_df['actual_fs_path'].dropna())
        
        all_actual_files_in_dirs = set()
        for directory in all_dirs_from_db:
            all_actual_files_in_dirs.update(self.scan_filesystem(directory))
            
        extra_files_on_fs = all_actual_files_in_dirs - all_fs_paths_found
        
        final_report_df = melted_df
        if extra_files_on_fs:
            self.logger.info(f"Found {len(extra_files_on_fs)} extra files.")
            extra_files_df = pd.DataFrame({
                'filepath_to_check': list(extra_files_on_fs),
                'status': self.FileStatus.EXTRA_ON_FILE_SYSTEM,
                'source_column': 'FILESYSTEM'
            })
            final_report_df = pd.concat([melted_df, extra_files_df], ignore_index=True)
        else:
            self.logger.info("No extra files found on the filesystem.")
            
        return final_report_df
    
    def write_to_file(self, df: pd.DataFrame):
        if not self.csv_file:
            return
        
        if df.empty:
            self.logger.warning(f"Skipping file write, no data to write to {self.csv_file}")
            return
        
        try:
            self.logger.info(f"Writing output to {self.csv_file}...")
            df.to_csv(
                self.csv_file,
                sep = "|",
                index=False,
                encoding="utf-8"
            )
        except Exception as e:
            self.logger.error(f"Error writing to file {self.csv_file}: {e}")

    def write_to_excel(self, df: pd.DataFrame, sheet_name = 'Compare Report'):
        if not self.excel_file:
            return
        
        if df.empty:
            self.logger.warning(f"Skipping file write, no data to write to {self.excel_file}")
            return
        
        try:
            output_path = self.excel_file
            if os.path.isdir(output_path):
                default_filename = f"comparison_report_{self.template_type}_{self.zone}.xlsx"
                output_path = os.path.join(output_path, default_filename)
                self.logger.warning(f"Output path is a directory. Writing to default file: {output_path}")
            
            output_dir = os.path.dirname(output_path)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
                self.logger.info(f"Created directory: {output_dir}")

            self.logger.info(f"Writing output to {output_path}...")
            df.to_excel(
                output_path,
                sheet_name=sheet_name,
                index=False,
                engine="openpyxl"
            )
            self.logger.info("Excel write successful.")

        except ImportError:
            self.logger.error("Could not write to Excel. The 'openpyxl' library is required. Please install it using 'pip install openpyxl'.")

        except Exception as e:
            self.logger.error(f"Error writing to Excel file {self.excel_file}: {e}", exc_info=True)

    # def get_status(self, row):
    #     if row['on_filesystem']:
    #         return 'OK'
    #     else:
    #         return 'FOUND ON DB BUT MISSING FROM FILESYSTEM'
        
    def get_query(self):
        raise NotImplementedError("Subclasses must implement this method to provide a SQL query.")
        
    def run(self):
        try:
            query = self.get_query()
            if not query:
                self.logger.error("No query provided by the subclass. Aborting.")
                return
            
            df_from_db = self.execute_query(query)
            if df_from_db.empty:
                self.logger.warning("Execution finished early as no data was returned from DB.")
                return
            
            self.logger.info("--- Result from template_mstr ---")
            final_report = self.perform_comparison(df_from_db)
            
            self.logger.info("--- Final Comparison Report ---")
            self.write_to_file(final_report)
            self.write_to_excel(final_report)

            self.logger.info(f"Execution for {self.__class__.__name__} completed.")
            return final_report
        except ValueError as e:
            self.logger.error(f"A validation error occurred, aborting execution: {e}")
            return None

class ComparePstCrt(CompareFile):
    def __init__(self, args:argparse.Namespace):
        super().__init__(args)
        self.logger.info("--- Initialized for PSTCRT Comparison ---")

    def get_query(self) -> str:
        self.logger.info("Providing PSTCRT-specific query.")
        return f"""
        SELECT DISTINCT template_name, 
        script_path, 
        script_name,
        script_config_file  
        FROM ks{self.zone}_fwkdb.template_mstr;
        """
    
    def prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("Preparing PSTCRT dataframe...")

        df = self._build_full_file_name(df, 'pstcrt_script_full_name', 'script_path', 'script_name')
        df = self._build_full_file_name(df, 'pstcrt_script_config_file', 'script_path', 'script_config_file')

        col_to_drop = ['script_path', 'script_name', 'script_config_file']
        return df.drop(columns= col_to_drop)

    
    def get_comparison_columns(self) -> List[str]:
        return ['pstcrt_script_full_name', 'pstcrt_script_config_file']

class CompareOutbound(CompareFile):
    def __init__(self, args:argparse.Namespace):
        super().__init__(args)

    def get_query(self) -> str:
        self.logger.info("Providing OUTBOUND-specific query.")
        return f"""
        select workflow_nm, 
        obd_hql_path,
        obd_hql_nm,
        header_file_nm, 
        footer_file_nm,
        fix_len_config_file_nm,
        special_script_full_path
        from ks{self.zone}_fwkdb.outbound_file_mstr;
        """
    
    def prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        self.logger.info("Preparing Outbound dataframe...")
        df = self._build_full_file_name(df, 'obd_script_full_name', 'obd_hql_path', 'obd_hql_nm')
        df = self._build_full_file_name(df, 'obd_header_full_name', 'obd_hql_path', 'header_file_nm')
        df = self._build_full_file_name(df, 'obd_footer_full_name', 'obd_hql_path', 'footer_file_nm')
        df = self._build_full_file_name(df, 'obd_fix_len_full_name', 'obd_hql_path', 'fix_len_config_file_nm')
        df['obd_special_script_full_name'] = df['special_script_full_path'].fillna(None)

        col_to_drop = ['obd_hql_path','obd_hql_nm', 'header_file_nm', 'footer_file_nm',
                       'fix_len_config_file_nm', 'special_script_full_path']
        return df.drop(columns= col_to_drop)
    
    def get_comparison_columns(self) -> List[str]:
        return ['obd_script_full_name',
                'obd_header_full_name',
                'obd_footer_full_name',
                'obd_fix_len_full_name', 
                'obd_special_script_full_name'
                ]

def main():
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_filename = f"validate_file_path_case_sensitive_log_{timestamp_str}.log"
    full_log_path = os.path.join(log_dir, log_filename)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(full_log_path, mode='w'),
            logging.StreamHandler()
        ]
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mainEnvFile", required=True)
    parser.add_argument("-env", "--envFile", required=True)
    parser.add_argument("-e", "--entity", required=True)
    parser.add_argument("-z", "--zone", required=True)
    template_type_choices = ["PSTCRT", "OUTBOUND"]
    parser.add_argument("-t",
        "--templateType",
        choices=template_type_choices,
        default="PSTCRT",
        help=f"Specify the template type. Must be one of: {', '.join(template_type_choices)}."
    )
    parser.add_argument(
        "-csv", 
        "--csvFile", 
        required=False,
        help="Path to the output file. If not provided, result will only be printed to the console."
    )
    parser.add_argument(
        "-excel", 
        "--excelFile", 
        required=False,
        help="Path to the Excel output file. Requires 'openpyxl' to be installed."
    )
    parser.add_argument(
        "--ignore-ext",
        nargs='+',
        default=['*.ds_store', '*.bak*', '*bk*', '*.log*', '*.nfs*', '*.py*', '*.tmp*', '*.temp*'],
        help="Space-separated list of file extensions or filenames to ignore when checking for extra files. (e.g., .log .tmp .DS_Store)"
    )

    args: argparse.Namespace = parser.parse_args()
    logging.info(f"Logging to file: {full_log_path}")
    logging.info(f"Ignoring extensions for extra files: {args.ignore_ext}")
    comparison_task: CompareFile = None

    if args.templateType == "PSTCRT":
        comparison_task = ComparePstCrt(args)
    elif args.templateType == "OUTBOUND":
        comparison_task = CompareOutbound(args)

    if comparison_task:
        final_df = comparison_task.run()

        if final_df is not None and not final_df.empty:
            logging.info("--- Final Comparison Report ---")
            # logging.info(final_df.to_string())
    else:
        logging.error(f"Error: Unknown template type '{args.templateType}'")



if __name__ == '__main__':
    main()

    # -m /d/git/BAY_Upgrade_CDP_7.1.9/config/main_env_config -env /d/git/BAY_Upgrade_CDP_7.1.9/config/env_config -e kseim -z dev
    # -m D:\git\BAY_Upgrade_CDP_7.1.9\config\main_env_config -env D:\git\BAY_Upgrade_CDP_7.1.9\config\env_config -e kseim -z uat9 -t PSTCRT -csv "D:\git\BAY_Upgrade_CDP_7.1.9\output\test_output.csv" -excel "D:\git\BAY_Upgrade_CDP_7.1.9\output\"
    # -m 