from datetime import date, datetime, timedelta
import os
import argparse
import configparser
import importlib.util
import mysql.connector
from mysql.connector import Error
import pymysql

class CompareFile():
    def __init__(self, args:argparse.Namespace): 
        self.main_env_file = args.mainEnvFile
        self.env_file = args.envFile
        self.sep = os.sep
        self.env = {}
        self.init_env_config(self.main_env_file)
        self.init_env_config(self.env_file)        
        self.fwk_config = self.env["db_config_file"]
        self.database_config = self.get_database_config()

        self.start = datetime.now()
        self.end = datetime.now()
        self.duration = self.end - self.start

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
        spec = importlib.util.spec_from_file_location(
            "module.name",
              #f"{main_cryptography}{self.sep}decryption_string.py"
              "D:\\git\\BAY_Upgrade_CDP_7.1.9\\config\\decryption_string.py"
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
        ) 
        return conn 
    
    def select_template_file_path(self):
        sql=f"""select template_name
        , script_path, script_name
        , script_path||'/'||script_name as full_name
        , script_config_file  
        from ks{str(self.zone)}_fwkdb.template_mstr;
        """

    
    def get_filenames_from_db():
        print("กำลังเชื่อมต่อฐานข้อมูล MariaDB...")
        filenames_from_db = set()
        try:
            with mysql.connector.connect(**db_config) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(SQL_QUERY)
                    print(f"กำลังดึงข้อมูลจาก Query: {SQL_QUERY}")
                    for row in cursor:
                        filenames_from_db.add(row[0])
            print(f"ดึงข้อมูลจากฐานข้อมูลสำเร็จ: พบ {len(filenames_from_db)} รายการ")
            return filenames_from_db
        except Error as e:
            print(f"เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล: {e}")
            return None
        


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mainEnvFile", required=True)
    parser.add_argument("-env", "--envFile", required=True)
    parser.add_argument("-e", "--entity", required=True)
    parser.add_argument("-z", "--zone", required=True)

    args: argparse.Namespace = parser.parse_args()
    compare_file = CompareFile(args)
    compare_file.get_database_config()
    # compare_file.execute()

if __name__ == '__main__':
    main()

    # -m /d/git/BAY_Upgrade_CDP_7.1.9/config/main_env_config -env /d/git/BAY_Upgrade_CDP_7.1.9/config/env_config -e kseim -z dev
    # -m D:\git\BAY_Upgrade_CDP_7.1.9\config\main_env_config -env D:\git\BAY_Upgrade_CDP_7.1.9\config\env_config -e kseim -z dev