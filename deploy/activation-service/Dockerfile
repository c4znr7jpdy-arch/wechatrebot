FROM python:3.13-alpine@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/activation_service/requirements.txt services/activation_service/requirements.txt
RUN python -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: \
    --requirement services/activation_service/requirements.txt

COPY services/activation_service/*.py services/activation_service/
COPY src/wechatrebot_manager/__init__.py src/wechatrebot_manager/json_io.py src/wechatrebot_manager/
COPY src/wechatrebot_manager/licensing/__init__.py src/wechatrebot_manager/licensing/protocol.py src/wechatrebot_manager/licensing/contact.py src/wechatrebot_manager/licensing/

USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8091/health',timeout=3); assert json.load(r)['data']['status']=='ok'"
ENTRYPOINT ["python", "-m", "services.activation_service", "--data-dir", "/data"]
CMD ["serve", "--host", "0.0.0.0", "--rate-per-minute", "6000"]
