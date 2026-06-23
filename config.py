import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    if not SECRET_KEY:
        raise RuntimeError('SECRET_KEY environment variable is required. Generate one with: python -c "import secrets; print(secrets.token_hex(32))"')
    if SECRET_KEY == 'skillsync-super-secret-key':
        import warnings
        warnings.warn('SECRET_KEY is set to the default placeholder! Generate a unique key for production: python -c "import secrets; print(secrets.token_hex(32))"')
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        'sqlite:///skillsync.db'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.getenv('FLASK_ENV') != 'development'
    PERMANENT_SESSION_LIFETIME = 86400
