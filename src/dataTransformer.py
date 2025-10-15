import gzip
import logging
import re
from pathlib import Path
from typing import Iterator, List, Dict

import pandas as pd
from sqlalchemy import create_engine, text
from decouple import config


logger = logging.getLogger("pubchem.dataTransformer")


class DataTransformer:
    """Stream SDF.gz files from DUMP_DIR, extract basic blocks, insert rows to MySQL,
    then deduplicate the `Pubchem` table by filename+cid.

    The decompression/processing is deliberately streaming to avoid loading entire
    .gz files into memory.
    """

    def __init__(self, dump_dir: str = None, db_url: str = None, batch_size: int = 1000):
        self.dump_dir = Path(dump_dir or config("DUMP_DIR"))
        self.batch_size = int(config("BULK_BATCH", default=batch_size))

        # Build SQLAlchemy URL if not provided
        if db_url is None:
            user = config("DB_USERNAME", default=config("DB_USER", default="admin"))
            pwd = config("DB_PASS")
            host = config("DB_HOST", default="127.0.0.1")
            port = config("DB_PORT", default="3306")
            dbname = config("DB_NAME", default="photonics")
            db_url = f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{dbname}"

        self.engine = create_engine(db_url)

        # simple regex to find the counts line in molfile
        self._counts_re = re.compile(r"^\s*(\d+)\s+(\d+)\s")

    def iter_molecules_from_gz(self, gz_path: Path) -> Iterator[str]:
        """Yield molecule blocks (text) terminated by $$$$ from a .sdf.gz file."""
        with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as fh:
            buf: List[str] = []
            for line in fh:
                buf.append(line)
                if line.strip() == "$$$$":
                    yield "".join(buf)
                    buf = []
            if buf:
                yield "".join(buf)

    def analyze_molecule_blocks(self, mol_text: str) -> Dict[str, int]:
        """Return atom and bond counts and flags whether blocks exist."""
        atom_count = bond_count = 0
        # counts line is normally within the first ~20 lines
        for ln in mol_text.splitlines()[:40]:
            m = self._counts_re.match(ln)
            if m:
                atom_count = int(m.group(1))
                bond_count = int(m.group(2))
                break
        return {
            "atom_count": atom_count,
            "bond_count": bond_count,
            "has_atom_block": atom_count > 0,
            "has_bond_block": bond_count > 0,
        }

    def _extract_atom_block_lines(self, mol_text: str) -> List[str]:
        """Return the raw atom block lines from the molfile portion of mol_text.

        The atom block appears immediately after the counts line and contains atom_count lines.
        """
        lines = mol_text.splitlines()
        for idx, ln in enumerate(lines[:40]):
            m = self._counts_re.match(ln)
            if m:
                atom_count = int(m.group(1))
                start = idx + 1
                return lines[start:start + atom_count]
        return []

    def _compute_empirical_formula(self, atom_lines: List[str]) -> str:
        """Compute a simple empirical formula (Hill order) from atom block lines.

        This counts element symbols found in the typical Molfile atom column (4th token).
        """
        from collections import Counter
        elem_re = re.compile(r"^([A-Z][a-z]?)")
        counts = Counter()
        for ln in atom_lines:
            parts = re.split(r"\s+", ln.strip())
            if len(parts) >= 4:
                cand = parts[3]
                m = elem_re.match(cand)
                if m:
                    counts[m.group(1)] += 1
                    continue
            # fallback: search for first element-like token
            m = re.search(r"\b([A-Z][a-z]?)\b", ln)
            if m:
                counts[m.group(1)] += 1

        # Build Hill order: C, then H, then alphabetical other elements
        def fmt(e, n):
            return f"{e}{n if n > 1 else ''}"

        parts = []
        if 'C' in counts:
            parts.append(fmt('C', counts.pop('C')))
        if 'H' in counts:
            parts.append(fmt('H', counts.pop('H')))
        for e in sorted(counts.keys()):
            parts.append(fmt(e, counts[e]))
        return ''.join(parts)

    def extract_pubchem_names(self, mol_text: str) -> Dict[str, str]:
        """Parse SDF property fields for requested PUBCHEM names.

        Returns a dict with keys 'pubchem_iupac_cas_name' and 'pubchem_iupac_openeye_name'
        (values may be None if not present).
        """
        cas = None
        openeye = None
        # SDF property blocks use lines like: >  <PUBCHEM_IUPAC_CAS_NAME>
        prop_re = re.compile(r"^>\s*<([^>]+)>\s*$")
        lines = mol_text.splitlines()
        i = 0
        while i < len(lines):
            m = prop_re.match(lines[i])
            if m:
                key = m.group(1).strip()
                # next line(s) until blank are the value
                j = i + 1
                val_lines = []
                while j < len(lines) and lines[j].strip() != "":
                    val_lines.append(lines[j])
                    j += 1
                val = "\n".join(val_lines).strip() if val_lines else None
                if key == "PUBCHEM_IUPAC_CAS_NAME":
                    cas = val
                elif key == "PUBCHEM_IUPAC_OPENEYE_NAME":
                    openeye = val
                i = j
            else:
                i += 1
        return {
            "pubchem_iupac_cas_name": cas,
            "pubchem_iupac_openeye_name": openeye,
        }

    def process_file_to_db(self, gz_path: Path, table_name: str = "Pubchem") -> int:
        """Stream and process one gz file, inserting rows in batches. Returns inserted count."""
        inserted = 0
        batch: List[Dict] = []
        try:
            for mol_text in self.iter_molecules_from_gz(gz_path):
                blocks = self.analyze_molecule_blocks(mol_text)
                if not (blocks["has_atom_block"] or blocks["has_bond_block"]):
                    logger.info("%s contains no blocks; skipping a molecule", gz_path.name)
                    continue
                batch.append({
                    "filename": gz_path.name,
                    "atom_count": blocks["atom_count"],
                    "bond_count": blocks["bond_count"],
                    # store a short preview to avoid huge DB payloads; extend if needed
                    "mol_preview": mol_text[:2000],
                    **self.extract_pubchem_names(mol_text),
                    "chem_comp": self._compute_empirical_formula(self._extract_atom_block_lines(mol_text)),
                })

                if len(batch) >= self.batch_size:
                    df = pd.DataFrame(batch)
                    df.to_sql(table_name, con=self.engine, if_exists="append", index=False, method="multi")
                    inserted += len(batch)
                    logger.info("Inserted %d rows from %s", len(batch), gz_path.name)
                    batch = []

            if batch:
                df = pd.DataFrame(batch)
                df.to_sql(table_name, con=self.engine, if_exists="append", index=False, method="multi")
                inserted += len(batch)
                logger.info("Inserted %d rows from %s", len(batch), gz_path.name)

        except Exception as e:
            logger.exception("Failed to unzip %s: %s", gz_path, e)

        return inserted

    def dedupe_table(self, table_name: str = "Pubchem"):
        """Remove duplicate rows from Pubchem table, keeping the earliest id per (filename,cid).

        This implementation assumes the table has at least (id, filename, cid) columns. If
        `cid` doesn't exist or you used a different schema, adjust accordingly.
        """
        # Safe dedupe using temporary table - adjust SQL for MySQL compatibility
        dedupe_sql = text(
            f"""
            DELETE p FROM `{table_name}` p
            INNER JOIN (
                SELECT filename, mol_preview, MIN(id) as keep_id
                FROM `{table_name}`
                GROUP BY filename, mol_preview
                HAVING COUNT(*) > 1
            ) dup ON dup.filename = p.filename AND dup.mol_preview = p.mol_preview
            WHERE p.id <> dup.keep_id;
            """
        )
        with self.engine.begin() as conn:
            try:
                conn.execute(dedupe_sql)
                logger.info("Removed duplicate rows from Pubchem table")
            except Exception:
                logger.exception("Failed to remove duplicates; check table schema and adjust SQL")

    def run_all(self, table_name: str = "Pubchem"):
        """Process all .sdf.gz files found in the dump_dir once, then dedupe DB."""
        files = sorted(self.dump_dir.glob("*.sdf.gz"))
        total = 0
        for gz in files:
            inserted = self.process_file_to_db(gz, table_name=table_name)
            total += inserted
        logger.info("Total inserted rows: %d", total)
        # Run dedupe step after all inserts
        self.dedupe_table(table_name=table_name)
