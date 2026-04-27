with open('crud/webhook.py') as f:
    content = f.read()

# Update types and imports
content = content.replace('from sqlalchemy.orm import Session', 'from sqlalchemy.ext.asyncio import AsyncSession')
content = content.replace('Session', 'AsyncSession')
content = content.replace('from models import WebhookEvent, get_session', 'from models import WebhookEvent, get_session\nfrom sqlalchemy import select, func, distinct')

# Update helper functions
content = content.replace('session.query(WebhookEvent)', 'await session.execute(select(WebhookEvent))')

# We need a robust regex or AST approach.
# Since python string replacement is tricky for complex queries like `.filter().order_by().first()`,
# it's better to rewrite the specific DB functions.


def rewrite_query_first(func_text):
    # Rewrites standard session.query(...).filter(...).order_by(...).first()
    # It turns `session.query(Model)` into `await session.execute(select(Model))`
    # and `.first()` into `.scalar_one_or_none()` or `.scalars().first()`

    # We will do manual replacement for the 5 CRUD functions here to be safe and accurate.
    pass
