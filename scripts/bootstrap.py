"""Shared startup command for both Docker services."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

import django

django.setup()

from core.bootstrap import bootstrap_database

if __name__ == '__main__':
    bootstrap_database()
