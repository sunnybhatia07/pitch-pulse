import logging
import os
import sys
from datetime import datetime

# Ensure logs directory exists
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

def get_logger(name: str, log_level: str = "INFO") -> logging.Logger:
    """
    Returns a configured logger with both Console and File handlers.
    """
    logger = logging.getLogger(name)
    
    # Avoid attaching duplicate handlers if get_logger is called multiple times
    if logger.handlers:
        return logger

    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Define common format
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console Handler (Standard Output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler (Daily rolling log file)
    log_file = os.path.join(LOGS_DIR, f"pitch_pulse_{datetime.now().strftime('%Y_%m_%d')}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger