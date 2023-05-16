FROM python:3-slim-buster as wheel_builder

ARG CLEARCACHE=1

RUN pip3 install poetry

COPY backend ./backend

RUN cd backend && poetry build && mkdir /out/ && cp dist/*.whl /out/

# Final image
FROM python:3-slim-buster

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg nginx && apt-get clean && rm -rf /var/lib/apt/lists/* && \
  useradd -r python && usermod -g www-data python && mkdir /data && chown python:www-data /data

COPY --from=wheel_builder /out/*.whl /tmp/
RUN pip3 install --no-cache-dir /tmp/*.whl supervisor && rm /tmp/*.whl

COPY frontend/src /var/www/html
COPY default_site /etc/nginx/sites-available/default
COPY supervisord.conf /etc/supervisor/supervisord.conf

# Establish the runtime user (with no password and no sudo)
RUN useradd -m jbot
USER jbot

EXPOSE 8080

ADD start.sh /
RUN chmod +x /start.sh

CMD bash start.sh
