# Generated by https://smithery.ai. See: https://smithery.ai/docs/config#dockerfile
# Use a Python base image with hatchling for building
FROM python:3.11-slim as builder

# Set work directory
WORKDIR /app

# Install build system
RUN pip install hatchling

# Copy project files
COPY pyproject.toml README.md /app/
COPY src /app/src

# Build the project wheel
RUN python -m build

# Use a separate environment for the final image
FROM python:3.11-slim

WORKDIR /app

# Install the package
COPY --from=builder /app/dist/*.whl /app/
RUN pip install /app/*.whl

# Define default command
ENTRYPOINT ["python", "-m", "mcp_server_reddit"]
