import os

with open('core/utils.py') as f:
    lines = f.readlines()

dataclasses_start = -1
for i, line in enumerate(lines):
    if line.startswith('class DuplicateCheckResult'):
        dataclasses_start = i - 1
        break

db_functions_start = -1
for i, line in enumerate(lines):
    if line.startswith('def _query_last_beyond_window_event'):
        db_functions_start = i
        break

lock_func_start = -1
for i, line in enumerate(lines):
    if line.startswith('@asynccontextmanager\n') and 'def processing_lock' in lines[i+1]:
        lock_func_start = i
        break

# Extract the dataclasses and DB functions
dataclasses_code = "".join(lines[dataclasses_start:dataclasses_start+15]) # approx length of the two dataclasses
db_code = "".join(lines[db_functions_start:lock_func_start])

# Create crud/webhook.py
crud_content = f"""import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import Config
from core.logger import logger
from db.session import session_scope
from models import WebhookEvent, get_session

# We will import the purely utility functions back from core.utils
from core.utils import generate_alert_hash, _decode_raw_payload, _normalize_headers

WebhookData = dict[str, Any]
HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]

{dataclasses_code}

{db_code}
"""

os.makedirs('crud', exist_ok=True)
with open('crud/webhook.py', 'w') as f:
    f.write(crud_content)

# Now we need to remove these from core.utils.py
new_utils_content = "".join(lines[:dataclasses_start]) + "".join(lines[dataclasses_start+15:db_functions_start]) + "".join(lines[lock_func_start:])

with open('core/utils.py', 'w') as f:
    f.write(new_utils_content)

