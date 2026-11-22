# Builds sqlglot's per-test coverage map during the image build (allowed: pinned commit + PyPI).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY fetch_sqlglot.py /opt/tools/fetch_sqlglot.py
RUN python /opt/tools/fetch_sqlglot.py /opt/sqlglot
RUN pip install --no-cache-dir pytest==9.2.0 pytest-timeout==2.4.0 pytest-xdist==3.8.0 execnet==2.1.2 \
    iniconfig==2.3.1 pluggy==1.7.0 packaging==26.3 duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 \
    python-dateutil==4.0.0 pytz==2026.6 tzdata==2026.6 typing_extensions==4.16.0 coverage pytest-cov
COPY mapplugin.py build_map.py tests.json /opt/tools/
RUN mkdir -p /app/maps && cd /opt/sqlglot \
    && PYTHONPATH=/opt/sqlglot:/opt/tools SKIP_INTEGRATION=0 python /opt/tools/build_map.py /opt/tools/tests.json /app/maps \
    || echo "map build failed; selector will fall back to running everything"
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app /opt/sqlglot
CMD ["python", "/app/selector.py"]
