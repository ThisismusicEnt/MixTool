# cleanup.py
import os
import time
import logging

MAX_AGE_SECONDS = 1800  # 30 min

def cleanup_older_files(folder):
    now = time.time()
    for f in os.listdir(folder):
        f_path = os.path.join(folder, f)
        if os.path.isfile(f_path):
            age = now - os.path.getmtime(f_path)
            if age > MAX_AGE_SECONDS:
                try:
                    os.remove(f_path)
                    logging.info(f"Removed old file: {f_path}")
                except Exception as e:
                    logging.error(f"Failed removing {f_path}: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Clean up processed/ and uploads/
    cleanup_older_files("processed")
    cleanup_older_files("uploads")
