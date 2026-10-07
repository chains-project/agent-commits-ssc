"""Render the current verified experiment report."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent/'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent/'scripts/hallucination'))
from final_report import main

if __name__ == '__main__':
    main()
