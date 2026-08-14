# Our IFC importer: the stock upstream image with this specklepy fork
# installed over it.
#
# Upstream's importer drops IfcRelAssociatesClassification entirely, so
# classification codes authored in the model (Uniclass, CCS, ...) never reach
# the Speckle object. Our fork adds them.
#
# Built by .github/workflows/build-ifc-import-image.yml, which supplies the
# wheel in dist/ next to this file. Only the specklepy wheel is replaced. The
# service wrapper (ifc_importer, main.py) and every other dependency stay
# exactly as upstream shipped them.
ARG SPECKLE_VERSION

FROM speckle/speckle-ifc-import-service:${SPECKLE_VERSION}

ARG WHEEL

# The upstream venv is uv-built with no pip in it, so borrow a uv binary.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY dist/${WHEEL} /tmp/${WHEEL}

USER root
# --no-deps is load-bearing: the image's dependency set is already resolved and
# pinned upstream, and letting the wheel re-resolve would pull bumps into a
# production image (its metadata wants a newer ifcopenshell than ships here).
RUN set -eu; \
    uv pip install --python /app/bin/python --no-deps --reinstall "/tmp/${WHEEL}"; \
    rm "/tmp/${WHEEL}"; \
    rm /usr/local/bin/uv; \
    # Fail the build here rather than at import time in production.
    /app/bin/python -c 'from speckleifc.property_extraction import _get_classifications'; \
    /app/bin/python -c 'import ifc_importer, main'
USER app
