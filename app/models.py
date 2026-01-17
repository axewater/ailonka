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
