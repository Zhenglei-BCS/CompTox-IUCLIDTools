# Engine-only image for batch/pipeline runs (no Streamlit, no NLP models):
#   docker build --target engine -t iuclid-engine .
#   docker run --rm -v "$PWD/work:/work" iuclid-engine convert --input /work/in.csv --mapping /work/mapping.json --output /work/out.i6z
FROM python:3.12-slim AS engine

WORKDIR /app
COPY requirements-engine.txt .
RUN pip install --no-cache-dir -r requirements-engine.txt \
 && useradd --create-home engine
COPY iuclid_engine/ iuclid_engine/
COPY entity_models/ entity_models/
COPY pipeline_config/ pipeline_config/
COPY data/IUCLID6_6_all_fields_update01.xlsx data/
# Recorded in every report/audit entry, e.g. --build-arg IUCLID_ENGINE_BUILD=$(git rev-parse HEAD)
ARG IUCLID_ENGINE_BUILD=unknown
ENV IUCLID_ENGINE_BUILD=${IUCLID_ENGINE_BUILD}
USER engine
ENTRYPOINT ["python", "-m", "iuclid_engine"]

# Full Streamlit app (default target)
FROM python:3.12-slim AS app

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file to the container
COPY requirements.txt .

# Install the required dependencies
RUN --mount=type=cache,target=/tmp/pip-cache,id=pip_cache \
    pip install --cache-dir /tmp/pip-cache -r requirements.txt \
 && pip freeze > requirements.freeze.txt \
 && python -c "import nltk; nltk.download('stopwords'); nltk.download('punkt') ; nltk.download('punkt_tab')"

# Copy the rest of the application code
COPY . .

# Expose the port that Streamlit will run on
EXPOSE 8506

# Command to run the application
CMD ["streamlit", "run", "ezmapper.py", "--server.port=8506", "--server.address=0.0.0.0"]
