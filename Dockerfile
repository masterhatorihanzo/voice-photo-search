FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests gunicorn dateparser
COPY app.py .
EXPOSE 8008
CMD ["gunicorn", "-b", "0.0.0.0:8008", "-w", "2", "app:app"]
