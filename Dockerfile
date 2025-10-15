# Minimal Dockerfile in case you want to include init SQL in image
FROM mysql:8
# Copy init SQL into image so it runs on first container start
COPY init.sql /docker-entrypoint-initdb.d/
