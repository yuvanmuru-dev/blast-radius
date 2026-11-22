# Same base image the organisers labeled with (its Python and SQLite decide test outcomes).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV BR_DIAG=8 PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# sqlglot at the pinned commit, straight from its own repository
RUN python - <<'PY'
import io, tarfile, urllib.request, os, shutil
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz").read()
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt", filter="data")
shutil.move(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
PY
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests || true
COPY selector/ /app/selector/
COPY maps/ /app/maps/
RUN python /app/selector/mkbase.py /opt/sqlglot /app/maps/fp_base.json && rm -rf /tmp/ov /tmp/fpb* \
 && chmod -R a+rX /app /opt/sqlglot && python /app/selector/selfcheck.py
CMD ["python", "/app/selector/main.py"]
