FROM python:3.9.6-slim

WORKDIR /app

RUN apt update && apt -y install curl

ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.1.11/supercronic-linux-amd64 \
	SUPERCRONIC=supercronic-linux-amd64 \
	SUPERCRONIC_SHA1SUM=a2e2d47078a8dafc5949491e5ea7267cc721d67c

RUN curl -fsSLO "$SUPERCRONIC_URL" \
	&& echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - \
	&& chmod +x "$SUPERCRONIC" \
	&& mv "$SUPERCRONIC" "/usr/local/bin/${SUPERCRONIC}" \
	&& ln -s "/usr/local/bin/${SUPERCRONIC}" /usr/local/bin/supercronic

COPY requirements.txt /app
RUN pip install -r requirements.txt

COPY crontab /app/crontab
COPY main.py /app

CMD supercronic -debug /app/crontab
