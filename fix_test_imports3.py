import re


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
        content = re.sub(fr'from core\.utils import(.*?){item}', fr'from crud.webhook import {item}\nfrom core.utils import\1', content)

    # Clean up empty lines
    content = re.sub(r'from core\.utils import\s*\n', '', content)
    content = re.sub(r'from core\.utils import \(\s*\)', '', content)
    content = content.replace('from core.utils import ,', 'from core.utils import ')
    content = content.replace(', ,', ',')
    
    with open(filepath, 'w') as f:
        f.write(content)

for file in ['core/webhook_security.py', 'services/ai_analyzer.py', 'tests/test_duplicate_window_strategy.py', 'tests/test_save_fallback_semantics.py', 'services/pipeline.py']:
    process_file(file)
