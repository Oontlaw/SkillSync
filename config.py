import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    if not SECRET_KEY:
        raise RuntimeError('SECRET_KEY environment variable is required. Generate one with: python -c "import secrets; print(secrets.token_hex(32))"')
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        'sqlite:///skillsync.db'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
