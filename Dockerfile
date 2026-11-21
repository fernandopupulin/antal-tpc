FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKIP_INTEGRATION=0 PYTHONHASHSEED=0 BR_CALIB=alt PYTHON_CPU_COUNT=2
WORKDIR /app
COPY engine/requirements.lock /app/engine/requirements.lock
RUN pip install --no-cache-dir -r /app/engine/requirements.lock
RUN python -c "import io,tarfile,urllib.request; url='https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa'; data=urllib.request.urlopen(url,timeout=120).read(); tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt',filter='data')" \
    && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot
COPY engine /app/engine
COPY entry.py workci.py strict_diff.py catalog.json /app/
RUN cd /opt/sqlglot \
    && python /app/engine/mksnap.py /opt/sqlglot /app/engine/snap.json \
    && python /app/engine/prep.py /app/engine/maps/maps.json.gz /app/engine/maps/maps.bin \
    && python -m compileall -q -j 4 /opt/sqlglot /app /usr/local/lib/python3.13 \
    && chmod -R a+rX /opt/sqlglot /app
ENTRYPOINT ["python", "/app/entry.py"]
