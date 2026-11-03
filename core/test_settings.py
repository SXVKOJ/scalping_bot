"""Isolated test database: never connect the test runner to a trading database."""
from .settings import *

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
EXCHANGE = 'bingx'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
