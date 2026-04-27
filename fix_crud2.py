import os


def process_file(filepath):
    if 'venv' in filepath or '.git' in filepath or '__pycache__' in filepath:
        return
    with open(filepath) as f:
        content = f.read()

    to_replace = [
        'get_all_webhooks', 'save_webhook_data', 'check_duplicate_alert', 
        'SaveWebhookResult', 'DuplicateCheckResult', '_save_to_file_fallback',
        'get_client_ip', 'WebhookData'
    ]
    
    for item in to_replace:
        content = content.replace(f'from core.utils import {item}', f'from crud.webhook import {item}')
        content = content.replace(f'from core.utils import (\n    {item},', f'from crud.webhook import {item}\nfrom core.utils import (')
        content = content.replace(f'from core.utils import {item}, ', f'from crud.webhook import {item}\nfrom core.utils import ')
        content = content.replace(f', {item}', '')
        content = content.replace(f'{item}, ', '')
    
    # Specific case for services/pipeline.py which imports processing_lock and save_webhook_data from core.utils
    content = content.replace('from core.utils import processing_lock\nfrom crud.webhook import save_webhook_data', 'from core.utils import processing_lock\nfrom crud.webhook import save_webhook_data')

    with open(filepath, 'w') as f:
        f.write(content)

for root, _, files in os.walk('.'):
    for file in files:
        if file.endswith('.py'):
            process_file(os.path.join(root, file))
