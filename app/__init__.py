import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize extensions
db = SQLAlchemy()
login_manager = LoginManager()

def create_app(enable_scheduler: bool = True):
    """Flask application factory"""
    app = Flask(__name__)

    # Configuration
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        f"postgresql://{os.getenv('DB_USERNAME')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Initialize extensions with app
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'Please log in to access this page.'

    # Import and register routes
    from app import routes
    app.register_blueprint(routes.bp)

    # Initialize scheduler for background sync (only for main process)
    if enable_scheduler and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        try:
            from app.services.scheduler_service import init_scheduler
            init_scheduler(app)
        except Exception as e:
            app.logger.warning(f"Failed to initialize scheduler: {e}")

    return app
