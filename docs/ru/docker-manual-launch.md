# Подробности запуска Docker без Compose

[Документация](./README.md) | [Корневой README](../../README.md)

Основной и рекомендуемый путь для полного приложения остается `docker compose up -d`: Compose сам собирает и связывает nginx, gateway и OCR. Если в задании или при отладке нужны явные команды `docker build` и `docker run`, можно запустить те же сервисы вручную:

```bash
docker build -f docker/ocr.Dockerfile -t ittm-ocr ./ocr
docker build -f docker/gateway.Dockerfile -t ittm-gateway .
docker build -f docker/nginx.Dockerfile -t ittm-nginx .
```

```bash
docker network create ittm-net
docker volume create ittm-ocr-python-packages
docker volume create ittm-ocr-easyocr-models

docker run -d --name ittm-ocr --network ittm-net \
  -e PORT=8000 \
  -e PYTHONPATH=/opt/ittm-python-packages \
  -e EASY_INSTALL_TARGET=/opt/ittm-python-packages \
  -e EASYOCR_MODULE_PATH=/models/easyocr \
  -v ittm-ocr-python-packages:/opt/ittm-python-packages \
  -v ittm-ocr-easyocr-models:/models/easyocr \
  ittm-ocr

docker run -d --name ittm-gateway --network ittm-net \
  -e PORT=3000 \
  -e OCR_URL=http://ittm-ocr:8000 \
  ittm-gateway

docker run -d --name ittm-nginx --network ittm-net \
  -p 127.0.0.1:3000:80 \
  -e GATEWAY_HOSTNAME=ittm-gateway \
  -e GATEWAY_INTERNAL_PORT=3000 \
  ittm-nginx
```

Проверка:

```bash
curl -fsS http://127.0.0.1:3000/api/health
```

CI/test OCR-образ собирается отдельным target'ом, чтобы production OCR-образ не содержал тесты:

```bash
docker build -f docker/ocr.Dockerfile --target test \
  --build-arg PYTHON_REQUIREMENTS=requirements-ci.txt \
  --build-arg OCR_INSTALL_CJK_FONTS=1 \
  -t ittm-ocr-ci ./ocr
```
