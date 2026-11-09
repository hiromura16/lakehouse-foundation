# Keep the Airflow package and Python ABI consistent with its constraints.
FROM apache/airflow:3.4.0-python3.12@sha256:818f94affc5bd7ce0be0cc04f2ace09aa8382e65190a73a08881229ff32700cb

USER airflow
# Verify the release constraints before installing the two explicit providers.
COPY --chown=airflow:root infra/orchestration/fetch_constraints.py /tmp/
RUN python /tmp/fetch_constraints.py
RUN pip install --no-cache-dir \
    'apache-airflow==3.4.0' \
    'apache-airflow-providers-standard==1.21.0' \
    'apache-airflow-providers-http==6.3.0' \
    --constraint /tmp/airflow-constraints.txt \
    && pip check
COPY --chown=airflow:root infra/orchestration/dags/ /opt/lakehouse/dags/
