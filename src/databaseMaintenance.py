import logging
import logging
from datetime import datetime
from decouple import config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from pathlib import Path


class DatabaseMaintenance:
    """Create MySQL database and ensure the Pubchem table exists.

    Behavior:
    - Attempt to create the database using the configured DB user (DB_USERNAME/DB_PASS).
    - If that user lacks privileges (e.g. Access denied), and `DB_ROOT_PASSWORD` is
      provided in the environment, retry database creation as root.
    - Ensure a `Pubchem` table exists in the target database.
    """

    def __init__(self, db_name: str = None):
        self.db_name = db_name or config("DB_NAME", default="pubchem_db")
        # Support both DB_USER and DB_USERNAME for backwards compatibility
        self.db_user = config("DB_USERNAME", default=config("DB_USER", default=None))
        self.db_pass = config("DB_PASS", default=config("DB_PASS", default=None))
        self.db_host = config("DB_HOST", default="127.0.0.1")
        self.db_port = config("DB_PORT", default="3306")

    def _engine(self, user: str = None, password: str = None, database: str = None):
        user = user or self.db_user
        password = password or self.db_pass
        db = database or "mysql"
        uri = f"mysql+pymysql://{user}:{password}@{self.db_host}:{self.db_port}/{db}"
        return create_engine(uri, echo=False)

    def ensure_database_and_table(self):
        root_pass = config("DB_ROOT_PASSWORD", default=None)

        def _create_database(engine):
            with engine.connect() as conn:
                conn.execute(text(
                    f"CREATE DATABASE IF NOT EXISTS `{self.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
                ))
                logging.info(f"Ensured database {self.db_name} exists")

        # First attempt with the configured user
        try:
            logging.info(f"Connecting to {self.db_host}:{self.db_port} as {self.db_user} to ensure database")
            engine = self._engine(database="mysql")
            _create_database(engine)
        except SQLAlchemyError as e:
            logging.error(f"Database error: {e}")
            # Try to detect access-denied-like errors and fallback to root if available
            err_text = str(e)
            if ("Access denied" in err_text or "(1044," in err_text) and root_pass:
                try:
                    logging.info("Retrying database creation as root user")
                    root_engine = self._engine(user="root", password=root_pass, database="mysql")
                    _create_database(root_engine)
                except SQLAlchemyError as e2:
                    logging.error(f"Root fallback failed: {e2}")
                    raise
            else:
                raise

        # Ensure table exists in the newly-created database and return which table to use.
        try:
            engine_db = self._engine(database=self.db_name)

            expected_cols = {
                "id",
                "filename",
                "cid",
                "atom_count",
                "bond_count",
                "mol_preview",
                "pubchem_iupac_cas_name",
                "pubchem_iupac_openeye_name",
                "chem_comp",
                "created_at",
            }

            def _create_table(conn, table_name: str):
                # Use MEDIUMTEXT for long name fields and chem_comp to avoid truncation
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS `{table_name}` (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        filename VARCHAR(255) NOT NULL,
                        cid BIGINT NULL,
                        atom_count INT NULL,
                        bond_count INT NULL,
                        mol_preview MEDIUMTEXT NULL,
                        pubchem_iupac_cas_name MEDIUMTEXT NULL,
                        pubchem_iupac_openeye_name MEDIUMTEXT NULL,
                        chem_comp MEDIUMTEXT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_filename (filename),
                        INDEX idx_cid (cid)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """))

            with engine_db.connect() as conn:
                # Check if a table named exactly 'Pubchem' exists and inspect its columns
                res = conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'Pubchem'"
                ), {"db": self.db_name})
                existing = {row[0] for row in res}

                if not existing:
                    # No existing Pubchem table: create it with desired schema
                    _create_table(conn, "Pubchem")
                    logging.info("Created new table Pubchem in database %s", self.db_name)
                    return "Pubchem"

                # Try to adjust existing Pubchem table to desired schema: widen types and add chem_comp
                try:
                    conn.execute(text(
                        "ALTER TABLE `Pubchem` MODIFY COLUMN `pubchem_iupac_cas_name` MEDIUMTEXT NULL;"
                    ))
                    conn.execute(text(
                        "ALTER TABLE `Pubchem` MODIFY COLUMN `pubchem_iupac_openeye_name` MEDIUMTEXT NULL;"
                    ))
                    conn.execute(text(
                        "ALTER TABLE `Pubchem` MODIFY COLUMN `mol_preview` MEDIUMTEXT NULL;"
                    ))
                    if 'chem_comp' not in existing:
                        conn.execute(text(
                            "ALTER TABLE `Pubchem` ADD COLUMN chem_comp MEDIUMTEXT NULL;"
                        ))
                except SQLAlchemyError:
                    logging.exception("Failed to ALTER existing Pubchem table to desired types/columns; will create timestamped table instead")

                # Re-inspect columns after attempted ALTER
                res2 = conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'Pubchem'"
                ), {"db": self.db_name})
                existing2 = {row[0] for row in res2}

                if expected_cols.issubset(existing2):
                    logging.info("Adjusted existing Pubchem table to desired schema; using Pubchem")
                    return "Pubchem"

                # Create a timestamped table name to avoid clobbering old schema
                now = datetime.now()
                ts = f"{now.year:04d}_{now.month:02d}_{now.day:02d}_{now.hour:02d}_{now.minute:02d}"
                table_name = f"Pubchem_{ts}"
                _create_table(conn, table_name)
                logging.info("Existing Pubchem schema differs; created new table %s", table_name)
                return table_name

        except SQLAlchemyError as e:
            logging.error(f"Database/table creation error: {e}")
            raise
