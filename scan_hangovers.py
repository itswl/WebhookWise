import os
import re

def scan():
    issues = []
    
    # 1. Get all async functions defined in the project
    async_funcs = set()
    for root, _, files in os.walk('.'):
        if 'venv' in root or '.git' in root or '.pytest' in root: continue
        for file in files:
            if not file.endswith('.py'): continue
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                for line in f:
                    m = re.match(r'^\s*async def ([a-zA-Z0-9_]+)', line)
                    if m:
                        async_funcs.add(m.group(1))

    # 2. Scan files for issues
    for root, _, files in os.walk('.'):
        if 'venv' in root or '.git' in root or '.pytest' in root: continue
        for file in files:
            if not file.endswith('.py'): continue
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                lines = f.readlines()
                
            for i, line in enumerate(lines):
                line_num = i + 1
                
                # Check A: session.query (sync SQLAlchemy 1.x)
                if 'session.query(' in line:
                    issues.append(f"{path}:{line_num} | session.query() leftover")
                    
                # Check B: Missing await for session methods
                for method in ['commit', 'flush', 'refresh', 'get', 'execute', 'delete', 'scalar', 'scalars']:
                    if re.search(r'(?<!await\s)(?<!\w)(session|fallback_session)\.' + method + r'\(', line):
                        # Some might be in sync scopes like test_db_connection, but let's log them to verify
                        if 'test_db_connection' not in path and 'init_db' not in path and 'migrations' not in path and 'test_' not in file:
                            issues.append(f"{path}:{line_num} | Missing await for {method}()")

                # Check C: Missing await for redis methods
                for method in ['get', 'set', 'setex', 'delete', 'eval', 'incr', 'expire']:
                    if re.search(r'(?<!await\s)(?<!\w)(redis|redis_client)\.' + method + r'\(', line):
                        issues.append(f"{path}:{line_num} | Missing await for redis.{method}()")

                # Check D: Old imports
                if re.search(r'(from|import) core\.(routes|models|services)', line):
                    issues.append(f"{path}:{line_num} | Old core.* import")
                    
                if 'crud.webhook_crud' in line:
                    issues.append(f"{path}:{line_num} | Old crud.webhook_crud import")

                # Check E: Async functions called without await
                for func in async_funcs:
                    # Match `func(` but not `await func(` or `def func(` or `add_task(func` or `target=func`
                    if re.search(r'(?<!await\s)(?<!def\s)(?<!\w)' + func + r'\(', line):
                        # Exclude some known non-awaited usages (callbacks, gathers, etc.)
                        if 'add_task' not in line and 'target=' not in line and 'run_until_complete' not in line and 'asyncio.run' not in line and 'patch' not in line and 'getattr' not in line:
                            # Ignore self-reference in definitions (e.g. `async def func():`) which we caught by `(?<!def\s)`, but might be `async def func(`
                            if not re.search(r'^\s*async\s+def\s+' + func + r'\(', line):
                                issues.append(f"{path}:{line_num} | Missing await for async function {func}()")

                # Check F: Double async/await
                if 'async async' in line:
                    issues.append(f"{path}:{line_num} | Double async")
                if 'await await' in line:
                    issues.append(f"{path}:{line_num} | Double await")

    if issues:
        print(f"Found {len(issues)} potential refactoring hangovers:")
        for issue in issues:
            print(issue)
    else:
        print("Clean! No obvious refactoring hangovers found.")

scan()
