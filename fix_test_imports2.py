import os


def process_file(filepath):
    with open(filepath) as f:
        content = f.read()

    content = content.replace('from api', 'from api')
    
    with open(filepath, 'w') as f:
        f.write(content)

for root, _, files in os.walk('.'):
    if 'venv' in root or '.git' in root or '__pycache__' in root:
        continue
    for file in files:
        if file.endswith('.py'):
            process_file(os.path.join(root, file))
