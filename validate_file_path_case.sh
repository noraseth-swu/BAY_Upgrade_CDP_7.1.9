#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

source /nfs/msa/dapscripts/ks/fwk/prd/config/env_config

datetime_start=$(date +"%Y%m%d_%H%M%S")
date_start=$(date +"%Y%m%d")


while getopts "d:" opt; do
  case $opt in    
    d)
        #echo "Option -d value: '$OPTARG'"
        input_database_name=${OPTARG}
        ;;
    \?)
        echo "Invalid option: -$OPTARG"
        ;;
  esac
done

# --- Configuration ---
DB_USER="your_db_user"
DB_PASS="your_db_password"
DB_HOST="your_db_host"
DB_NAME="your_database"
TABLE_NAME="file_records"
COLUMN_NAME="filepath"
MOUNT_POINT="/mnt/storeeasy"
WORKDIR="/tmp/migration_check"

# --- File Names ---
DB_FILES="db_files.txt"
ACTUAL_FILES="actual_files.txt"
REPORT_FILE="case_sensitivity_mismatch_report.txt"

# --- Main Logic ---
mkdir -p "$WORKDIR"
cd "$WORKDIR"

echo "===== STEP 1: Exporting from MariaDB ====="
mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -D"$DB_NAME" -sN \
      -e "SELECT ${COLUMN_NAME} FROM ${TABLE_NAME};" \
      | sed "s|^${MOUNT_POINT}/||" > "$DB_FILES"
if [ ! -s "$DB_FILES" ]; then echo "Error: DB export failed."; exit 1; fi
echo "Found $(wc -l < $DB_FILES) records in database."
echo "Done."
echo ""

echo "===== STEP 2: Generating file list from Filesystem (${MOUNT_POINT}) ====="
# Check if mount point exists and is mounted
if ! mountpoint -q "$MOUNT_POINT"; then
    echo "Error: Mount point ${MOUNT_POINT} is not mounted or does not exist."
    exit 1
fi
find "$MOUNT_POINT" -type f -printf "%P\n" > "$ACTUAL_FILES"
echo "Found $(wc -l < $ACTUAL_FILES) files on filesystem."
echo "Done."
echo ""

echo "===== STEP 3: Comparing lists (Case-Sensitive) ====="
echo "Sorting lists for comparison (this can take time)..."
LC_ALL=C sort "$DB_FILES" -o "${DB_FILES}.sorted"
LC_ALL=C sort "$ACTUAL_FILES" -o "${ACTUAL_FILES}.sorted"
echo "Sorting complete."
echo ""

echo "Finding discrepancies..."
# comm -23 shows lines unique to the first file (DB records not found on disk)
LC_ALL=C comm -23 "${DB_FILES}.sorted" "${ACTUAL_FILES}.sorted" > "$REPORT_FILE"
echo "Done."
echo ""

# --- Final Report ---
echo "=================================================="
echo "          MISMATCH REPORT SUMMARY"
echo "=================================================="

if [ -s "$REPORT_FILE" ]; then
    NUM_ISSUES=$(wc -l < "$REPORT_FILE")
    echo "WARNING: Found ${NUM_ISSUES} files that are in the database but could not be found"
    echo "on the filesystem with an exact case-sensitive match."
    echo ""
    echo "The full list is available in: ${WORKDIR}/${REPORT_FILE}"
    echo ""
    echo "First 10 mismatches found:"
    head -n 10 "$REPORT_FILE"
else
    echo "SUCCESS: All files listed in the database were found on the filesystem with a matching case."
fi
echo "=================================================="

# --- Cleanup ---
rm "${DB_FILES}.sorted" "${ACTUAL_FILES}.sorted"
echo "Script finished. Working directory: ${WORKDIR}"