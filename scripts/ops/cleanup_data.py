import asyncio
import sys
from pathlib import Path

# Add the project root directory to the path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from services.operations.data_maintenance import cleanup_old_data_by_policy

if __name__ == "__main__":
    asyncio.run(cleanup_old_data_by_policy())
