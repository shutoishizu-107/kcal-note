FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 配信するのは index.html / sw.js / manifest / icons のみ（アプリ側の許可リスト）。サーバーコードは配信されない。
COPY server ./server
COPY index.html sw.js manifest.webmanifest ./
COPY icons ./icons
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ENV APP_ENV=production DATABASE_PATH=/data/app.db
EXPOSE 8000
# SQLiteを使うためワーカーは1（スレッドで並列処理）。同期は時間がかかり得るためタイムアウトを長めに。
CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "120", "-b", "0.0.0.0:8000", "server.wsgi:app"]
