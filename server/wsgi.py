"""本番用WSGIエントリ。例: gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:8000 server.wsgi:app"""
from .app import create_app

app = create_app()
