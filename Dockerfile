FROM python:3.12-slim

# The slim image has no Chinese font, so burned-in Chinese subtitles would come out as boxes.
# fontconfig provides fc-list, which the tools page uses to pre-fill that font's name for burned-in subtitles.
# tzdata lets the TZ setting take effect for the times the server itself writes, such as a stream's planned start;
# the pages and scheduled downloads follow the browser's own time zone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fontconfig tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY videocap videocap
COPY .streamlit .streamlit
COPY docker-entrypoint.sh /usr/local/bin/

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /downloads /data \
    && chown app:app /downloads /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# Chinese characters are drawn the Taiwanese way, not with the Japanese shapes of the same font.
# The app's own yt-dlp updates are pip user installs; under /data they survive a recreated container.
ENV FC_LANG=zh-tw \
    PYTHONUSERBASE=/data/python \
    VIDEOCAP_IN_DOCKER=1 \
    VIDEOCAP_MODE=local \
    VIDEOCAP_OUTPUT_DIR=/downloads \
    VIDEOCAP_DATA_DIR=/data \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_RUN_ON_SAVE=false \
    PYTHONUNBUFFERED=1

EXPOSE 8501
VOLUME ["/downloads", "/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"

# The entrypoint starts as root to make the mounted folders writable, then runs the app as "app".
ENTRYPOINT ["docker-entrypoint.sh"]
# The launcher starts the download engine with the container, so subscriptions are checked and unfinished jobs come
# back after a restart even when nobody opens the page. Streamlit options can follow it, as with streamlit run.
CMD ["python", "-m", "videocap"]
