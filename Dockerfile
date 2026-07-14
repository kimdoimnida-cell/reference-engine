FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8765
# 파이프라인이 다운로드+전사로 최대 1분 → 타임아웃 넉넉히
CMD gunicorn --chdir server app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2
