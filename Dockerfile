# syntax=docker/dockerfile:1.7

FROM python:3.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PADDLE_HOME=/opt/paddle \
    OMP_NUM_THREADS=1

RUN apt-get update && apt-get install -y \
    build-essential \
    libgomp1 \
    libglib2.0-0 \
    libgl1 \
    poppler-utils \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

COPY pyproject.toml .

RUN pip install --upgrade pip setuptools wheel

RUN pip install -e ".[ocr,dev]"

COPY . .

RUN python - <<EOF
import paddle
import paddleocr

print("Paddle:", paddle.__version__)
print("PaddleOCR:", paddleocr.__version__)
EOF

EXPOSE 8000

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]