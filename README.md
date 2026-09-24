# Data Hub Services

## Configuration

All credentials are read from environment variables, loaded from `fastApi/.env`.
The copy of `fastApi/.env` in this repo is an empty template. Fill it in on the
server and never commit the real values.

Files the Docker build or runtime expects that are **not** in this repo:

- `postgresql-42.6.0.jar`: PostgreSQL JDBC driver (download from jdbc.postgresql.org)
- `/code/tif_vat_dbf_files/`: raster attribute tables bundled with layer downloads
