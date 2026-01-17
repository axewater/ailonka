import os
from flask_login import UserMixin
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet
from app import db, login_manager

# Initialize Argon2 password hasher
ph = PasswordHasher()


def get_encryption_key():
    """Get encryption key from environment"""
    key = os.getenv('ENCRYPTION_KEY')
    if key:
        return key.encode() if isinstance(key, str) else key
    # Fallback for development (not recommended for production)
    return Fernet.generate_key()

class User(UserMixin, db.Model):
    """User model for authentication"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    # Relationship to settings
    settings = db.relationship('UserSettings', backref='user', uselist=False, cascade='all, delete-orphan')

    def set_password(self, password):
        """Hash password using Argon2"""
        self.password_hash = ph.hash(password)

    def check_password(self, password):
        """Verify password using Argon2"""
        try:
            ph.verify(self.password_hash, password)
            # Check if rehashing is needed (Argon2 will update parameters over time)
            if ph.check_needs_rehash(self.password_hash):
                self.password_hash = ph.hash(password)
                db.session.commit()
            return True
        except VerifyMismatchError:
            return False

    def __repr__(self):
        return f'<User {self.username}>'


class UserSettings(db.Model):
    """User settings for API configuration"""
    __tablename__ = 'user_settings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    encrypted_api_key = db.Column(db.Text, nullable=True)
    selected_model = db.Column(db.String(100), default='claude-3-5-haiku-latest')

    def set_api_key(self, api_key: str):
        """Encrypt and store API key"""
        if api_key:
            f = Fernet(get_encryption_key())
            self.encrypted_api_key = f.encrypt(api_key.encode()).decode()
        else:
            self.encrypted_api_key = None

    def get_api_key(self) -> str | None:
        """Decrypt and return API key"""
        if self.encrypted_api_key:
            f = Fernet(get_encryption_key())
            return f.decrypt(self.encrypted_api_key.encode()).decode()
        return None

    def get_masked_key(self) -> str:
        """Return masked version of API key for display"""
        key = self.get_api_key()
        if key and len(key) > 12:
            return key[:8] + '*' * (len(key) - 12) + key[-4:]
        return ''

    def __repr__(self):
        return f'<UserSettings user_id={self.user_id}>'


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID for Flask-Login"""
    return User.query.get(int(user_id))


class Source(db.Model):
    """Shopping website source for scraping"""
    __tablename__ = 'sources'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    base_url = db.Column(db.Text, nullable=False)
    requires_javascript = db.Column(db.Boolean, default=False)
    sync_interval_hours = db.Column(db.Integer, default=24)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    next_sync_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='active')  # active, paused, error
    last_error = db.Column(db.Text, nullable=True)
    consecutive_failures = db.Column(db.Integer, default=0)
    selectors = db.Column(db.JSON, nullable=True)  # Stored CSS/XPath selectors from LLM
    selector_version = db.Column(db.Integer, default=0)  # Tracks selector regeneration
    created_at = db.Column(db.DateTime, default=db.func.now())

    # Relationships
    user = db.relationship('User', backref=db.backref('sources', lazy='dynamic'))
    products = db.relationship('Product', backref='source', lazy='dynamic', cascade='all, delete-orphan')
    sync_logs = db.relationship('SyncLog', backref='source', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Source {self.name}>'


class Product(db.Model):
    """Product tracked from a source"""
    __tablename__ = 'products'

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('sources.id'), nullable=False)
    external_id = db.Column(db.String(255), nullable=True)  # Site-specific product ID
    name = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=True)
    product_url = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.Text, nullable=True)
    current_price = db.Column(db.Numeric(10, 2), nullable=True)
    original_price = db.Column(db.Numeric(10, 2), nullable=True)
    currency = db.Column(db.String(10), default='USD')
    is_available = db.Column(db.Boolean, default=True)
    is_favorite = db.Column(db.Boolean, default=False)
    user_notes = db.Column(db.Text, nullable=True)
    first_seen_at = db.Column(db.DateTime, default=db.func.now())
    last_updated_at = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now())

    # Relationships
    price_history = db.relationship('PriceHistory', backref='product', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Product {self.name[:50]}>'


class PriceHistory(db.Model):
    """Historical price records for products"""
    __tablename__ = 'price_history'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    recorded_at = db.Column(db.DateTime, default=db.func.now())

    def __repr__(self):
        return f'<PriceHistory product_id={self.product_id} price={self.price}>'


class SyncLog(db.Model):
    """Sync operation logs for tracking and debugging"""
    __tablename__ = 'sync_logs'

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('sources.id'), nullable=False)
    started_at = db.Column(db.DateTime, default=db.func.now())
    completed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='running')  # running, success, failed
    products_found = db.Column(db.Integer, default=0)
    products_added = db.Column(db.Integer, default=0)
    products_updated = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text, nullable=True)
    llm_tokens_used = db.Column(db.Integer, default=0)
    selectors_regenerated = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f'<SyncLog source_id={self.source_id} status={self.status}>'
