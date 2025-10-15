import logging
import sys
from src.pubchem import PubChem
from src.core import parallel
from src.logging_utils import setup_logging
from src.databaseMaintenance import DatabaseMaintenance
from src.dataTransformer import DataTransformer

@parallel(min_cores=2, min_threads=2)
def download_dumps(files):
    """Download a batch of dump files in parallel."""
    pc = PubChem()  # Create fresh instance per worker (for session/UA rotation)
    return pc.download_all_current_full(files)

def main():
    # Set up logging first
    setup_logging('pubchem_main')
    
    pc = PubChem()
    
    # Get the list of files but don't download yet
    files = pc.list_current_full()
    if not files:
        logging.error("No files found to download.")
        return
    
    # Download in parallel with n-1 workers
    downloaded = download_dumps(files)
    if downloaded is None:
        logging.error("Download failed - insufficient system resources")
        return
    
    logging.info(f"Successfully downloaded {len(downloaded)} files")
    
    # Then you can query across all local dumps:
    # Run DB maintenance (create DB/table if needed)
    dbm = DatabaseMaintenance()
    table_name = dbm.ensure_database_and_table()

    # Transform SDFs and load into DB (streaming, batched)
    dt = DataTransformer()
    dt.run_all(table_name=table_name)
    #random_cids = pc.sample_random_cids(1000, 1, 2_000_000)
    #matches = pc.find_cids_in_dump(random_cids, max_matches=1000)
    #pc.export_matches_to_json(matches, "sample_output.json")

if __name__ == "__main__":
    main()

