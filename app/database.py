"""Flask-SQLAlchemy database engine for run-specific raw event storage."""

from flask_sqlalchemy import SQLAlchemy

# Raw_<run_id> and metadata_<run_id> tables are created explicitly by app.run_tables.
db = SQLAlchemy()
