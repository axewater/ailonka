# Ailonka - Developer Quickstart

## Tech Stack
- **Backend:** Flask 3.0, SQLAlchemy, Flask-Login
- **Database:** PostgreSQL (localhost, user: postgres, pass: postgres)
- **Frontend:** Jinja2 templates, Bootstrap 5.3, Bootstrap Icons
- **AI:** Anthropic SDK (Claude models)

## Quick Start
```bash
cd /var/www/ailonka
source venv/bin/activate
python run.py  # Runs on port 7777
```

Default login: `admin` / `admin`

## Project Structure
```
app/
├── __init__.py          # Flask app factory
├── models.py            # User, UserSettings models
├── routes.py            # All route handlers
├── services/
│   └── anthropic_service.py  # LLM API wrapper
└── templates/
    ├── base.html        # Layout with navbar
    ├── login.html
    ├── dashboard.html
    ├── settings.html    # API key config
    └── chat.html        # Chat interface
```

## Key Routes
| Route | Auth | Purpose |
|-------|------|---------|
| `/login` | No | User login |
| `/dashboard` | Yes | Main dashboard |
| `/settings` | Yes | Configure API key & model |
| `/chat` | Yes | Chat with LLM |
| `/api/chat` | Yes | POST - Send message |
| `/api/test-connection` | Yes | POST - Verify API key |

## AI Integration
- API keys stored encrypted (Fernet) in `user_settings` table
- Models: `claude-3-5-haiku-latest`, `claude-sonnet-4-20250514`, `claude-opus-4-5-20250514`
- Chat history stored in Flask session (cleared on logout)
- Service class: `app/services/anthropic_service.py`

## Environment Variables (.env)
```
SECRET_KEY=...
DB_USERNAME=postgres
DB_PASSWORD=postgres
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ailonka
ENCRYPTION_KEY=...  # Fernet key for API key encryption
```

## Database
Reset/init: `python init_db.py`

Tables: `users`, `user_settings`

## Adding New Features
- New routes go in `app/routes.py`
- New templates in `app/templates/`
- LLM calls go through `AnthropicService` in `app/services/anthropic_service.py`
- Protected routes use `@login_required` decorator
