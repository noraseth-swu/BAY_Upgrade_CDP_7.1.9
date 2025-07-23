import subprocess
import os
import datetime
import time
import threading
import logging
try:
    import psutil
except ImportError:
    print("="*60)
    print("❌ ERROR: The 'psutil' library is required for resource monitoring.")
    print("Please install it by running: pip install psutil")
    print("="*60)
    exit(1)
# ==========================================================
# Configuration
# ==========================================================
PYTHON_EXECUTABLE = "python"
MAIN_SCRIPT_PATH = "validate_file_path_case_sensitive.py" 
MAIN_ENV_FILE = "/nfs/msa/dapscripts/fwk/prd/config/main_env_config"
OUTPUT_DIR = "/nfs/msa/dapscripts/fwk/dev/migrate_storeeasy/log"
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
# ==========================================================
def setup_logger(log_dir, timestamp):
    """Sets up a logger that outputs to both console and a file."""
    log_filepath = os.path.join(log_dir, f"test_run_{timestamp}.log")
    logger = logging.getLogger('TestRunner')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - %(message)s')
        console_formatter = logging.Formatter('%(message)s')
        file_handler = logging.FileHandler(log_filepath, mode='w')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(file_formatter)
        
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(console_formatter)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
    return logger
def format_duration(seconds):
    if seconds < 1: return f"{seconds:.2f}s"
    if seconds < 60: return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)
    return f"{minutes}m {remaining_seconds}s"
def format_bytes(byte_count):
    if byte_count is None or byte_count == 0: return "0 B"
    power = 1024; n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while byte_count >= power and n < len(power_labels):
        byte_count /= power; n += 1
    return f"{byte_count:.2f} {power_labels[n]}B"

# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!!!!!!!!!!!!!! นี่คือฟังก์ชันที่แก้ไขตามคำสั่งแล้ว !!!!!!!!!!!!!!!!!!!!!!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
def monitor_resources(process_pid, stop_event, resource_log, logger):
    """Monitors CPU, Memory, and Disk I/O Rate (Corrected Version)."""
    try:
        p = psutil.Process(process_pid)
        p.cpu_percent(interval=None)
        start_time = time.monotonic()
        
        # --- [BUG FIX] START ---
        # 1. เริ่มต้นค่า "ครั้งที่แล้ว" ของ I/O ทั้งระบบเป็น 0
        last_total_read_bytes = 0
        last_total_write_bytes = 0

        # 2. อ่านค่าครั้งแรกเพื่อเป็น baseline ที่ถูกต้อง (รวม parent และ children)
        try:
            initial_io = p.io_counters()
            last_total_read_bytes = initial_io.read_bytes
            last_total_write_bytes = initial_io.write_bytes
            for child in p.children(recursive=True):
                child_io = child.io_counters()
                last_total_read_bytes += child_io.read_bytes
                last_total_write_bytes += child_io.write_bytes
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass 
        # --- [BUG FIX] END ---

        while not stop_event.is_set():
            time.sleep(1) # หน่วงเวลาก่อนวัดผลเพื่อให้เกิดผลต่าง
            try:
                if not p.is_running(): break
                
                elapsed_time = time.monotonic() - start_time
                cpu_usage = p.cpu_percent(interval=None)
                mem_mb = p.memory_info().rss / (1024 * 1024)
                
                # --- [BUG FIX] START ---
                # 3. คำนวณยอดรวมของ "ปัจจุบัน" ทั้งระบบใหม่ทุกครั้ง
                current_total_read_bytes = p.io_counters().read_bytes
                current_total_write_bytes = p.io_counters().write_bytes

                # รวมค่าจาก children ทั้งหมด
                for child in p.children(recursive=True):
                    try:
                        cpu_usage += child.cpu_percent(interval=None)
                        mem_mb += child.memory_info().rss / (1024 * 1024)
                        child_io = child.io_counters()
                        current_total_read_bytes += child_io.read_bytes
                        current_total_write_bytes += child_io.write_bytes
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                # 4. คำนวณ Rate จากผลต่างของ "ยอดรวม"
                read_rate_mb_s = (current_total_read_bytes - last_total_read_bytes) / (1024 * 1024)
                write_rate_mb_s = (current_total_write_bytes - last_total_write_bytes) / (1024 * 1024)
                
                # ป้องกันค่าติดลบหาก child process หายไป
                read_rate_mb_s = max(0, read_rate_mb_s)
                write_rate_mb_s = max(0, write_rate_mb_s)
                # --- [BUG FIX] END ---

                resource_log.append({
                    'time': elapsed_time, 'cpu': cpu_usage, 'mem_mb': mem_mb,
                    'read_mb_s': read_rate_mb_s, 'write_mb_s': write_rate_mb_s
                })
                
                # --- [BUG FIX] START ---
                # 5. อัปเดตค่า "ครั้งที่แล้ว" ให้เป็น "ยอดรวมของปัจจุบัน"
                last_total_read_bytes = current_total_read_bytes
                last_total_write_bytes = current_total_write_bytes
                # --- [BUG FIX] END ---
                
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
    except psutil.NoSuchProcess:
        logger.warning("   [Resource Monitor] Process finished before monitoring could start.")


def run_test_case(test_name, args, logger):
    start_time = time.monotonic()
    logger.info("="*80)
    logger.info(f"🚀 RUNNING TEST: {test_name}")
    logger.info(f"   Entity: {args.get('-e', 'N/A')}, Zone: {args.get('-z', 'N/A')}, Type: {args.get('-t', 'N/A')}")
    logger.info("-"*80)
    
    command = [PYTHON_EXECUTABLE, MAIN_SCRIPT_PATH]
    for key, value in args.items():
        if value is not None: command.extend([key, str(value)])
    logger.info(f"COMMAND: {' '.join(command)}\n")
    
    resource_log, monitor_thread, stop_monitoring = [], None, threading.Event()
    
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding='utf-8', errors='ignore'
        )
        monitor_thread = threading.Thread(
            target=monitor_resources, args=(process.pid, stop_monitoring, resource_log, logger)
        )
        monitor_thread.start()
        stdout_str, stderr_str = process.communicate()
    except FileNotFoundError:
        logger.critical(f"❌ FATAL ERROR: Cannot find '{PYTHON_EXECUTABLE}' or '{MAIN_SCRIPT_PATH}'.")
        return
    except Exception as e:
        logger.error(f"❌ UNEXPECTED ERROR during test execution: {e}", exc_info=True)
    finally:
        if monitor_thread and monitor_thread.is_alive():
            stop_monitoring.set(); monitor_thread.join()
        
        duration_seconds = time.monotonic() - start_time
        
        if 'process' in locals() and process.returncode != 0:
            logger.error(f"❌ STATUS: FAILED (Exit Code: {process.returncode})")
            logger.info("\n--- STDOUT (Output before error) ---\n" + stdout_str) 
            logger.info("\n--- STDERR (Error Messages) ---\n" + stderr_str)
        elif 'process' in locals():
            logger.info("✅ STATUS: SUCCESS")
            logger.info("\n--- STDOUT ---\n" + stdout_str)
        logger.info("\n--- 📊 Performance Summary ---")
        logger.info(f"   Execution Duration: {format_duration(duration_seconds)}")
        if resource_log:
            max_cpu_log = max(resource_log, key=lambda x: x['cpu'], default={})
            max_mem_log = max(resource_log, key=lambda x: x['mem_mb'], default={})
            max_read_rate_log = max(resource_log, key=lambda x: x['read_mb_s'], default={})
            max_write_rate_log = max(resource_log, key=lambda x: x['write_mb_s'], default={})
            avg_cpu = sum(log['cpu'] for log in resource_log) / len(resource_log)
            avg_mem = sum(log['mem_mb'] for log in resource_log) / len(resource_log)
            avg_read_rate = sum(log['read_mb_s'] for log in resource_log) / len(resource_log)
            avg_write_rate = sum(log['write_mb_s'] for log in resource_log) / len(resource_log)
            logger.info(f"   Peak CPU Usage:      {max_cpu_log.get('cpu', 0):.2f} % (at ~{format_duration(max_cpu_log.get('time', 0))})")
            logger.info(f"   Peak Memory Usage:   {max_mem_log.get('mem_mb', 0):.2f} MB (at ~{format_duration(max_mem_log.get('time', 0))})")
            logger.info(f"   Avg. CPU Usage:      {avg_cpu:.2f} %")
            logger.info(f"   Avg. Memory Usage:   {avg_mem:.2f} MB")
            logger.info("   " + "-" * 20)
            logger.info(f"   Peak Disk Read Rate: {max_read_rate_log.get('read_mb_s', 0):.2f} MB/s (at ~{format_duration(max_read_rate_log.get('time', 0))})")
            logger.info(f"   Peak Disk Write Rate:{max_write_rate_log.get('write_mb_s', 0):.2f} MB/s (at ~{format_duration(max_write_rate_log.get('time', 0))})")
            logger.info(f"   Avg. Disk Read Rate: {avg_read_rate:.2f} MB/s")
            logger.info(f"   Avg. Disk Write Rate:{avg_write_rate:.2f} MB/s")
        else:
            logger.warning("   Resource monitoring data is not available.")
            
        if 'process' in locals() and hasattr(process, 'pid'):
            try:
                final_io = psutil.Process(process.pid).io_counters()
                logger.info("   " + "-" * 20)
                logger.info(f"   Total Disk Read:     {format_bytes(final_io.read_bytes)}")
                logger.info(f"   Total Disk Write:    {format_bytes(final_io.write_bytes)}")
            except psutil.NoSuchProcess: pass
        logger.info("----------------------------------\n")

if __name__ == '__main__':
    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(OUTPUT_DIR, timestamp)
    logger.info(f"Log file for this run: {os.path.join(OUTPUT_DIR, f'test_run_{timestamp}.log')}")
    total_start_time = time.monotonic()
    for entity, config in ENTITY_CONFIG_MAP.items():
        zones = config['zones']
        env_file = config['env_file']
        if not zones:
            logger.info(f"\n- Skipping Entity '{entity}': No zones to test.")
            continue
        
        logger.info(f"\n==================== PROCESSING ENTITY: {entity.upper()} ({len(zones)} zones) ====================")
        
        for zone in zones:
            output_filename_base = f"ALL_{entity}_{zone}_{timestamp}"
            test_args = {
                "-m": MAIN_ENV_FILE, "-env": env_file, "-e": entity, "-z": zone, "-t": "ALL",
                "-csv": os.path.join(OUTPUT_DIR, f"{output_filename_base}.csv"),
                "-excel": os.path.join(OUTPUT_DIR, f"{output_filename_base}.xlsx")}
            
            test_name = f"ALL Validation for Entity: {entity}, Zone: {zone}"
            run_test_case(test_name, test_args, logger)
    
    total_duration_seconds = time.monotonic() - total_start_time
    logger.info("="*80)
    logger.info(f"🎉 All manual test runs finished in {format_duration(total_duration_seconds)}.")
    logger.info("="*80)