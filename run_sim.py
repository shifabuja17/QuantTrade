import json
import logging
from config import load_config
from strategy import StrategyEngine
from execution import ExecutionEngine

# Set log level
logging.basicConfig(level=logging.ERROR)

print("Starting simulations...")
