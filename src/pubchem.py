# src/pubchem.py
import requests
import re
import gzip
import json
import random
import sys
import logging
from pathlib import Path
from typing import Iterable, List, Set, Dict, Optional, Union, Tuple
from bs4 import BeautifulSoup
from decouple import config, Config as DecoupleConfig, RepositoryEnv

from .core import get_random_user_agent
from .logging_utils import setup_logging


class PubChem:
    def __init__(self, env_path: Optional[str] = None):
        # Set up logging
        self.log_file = setup_logging("pubchem")

        # PubChem + local cache settings (defaults provided)
        # If env_path is provided, use a dedicated decouple Config that reads that file only.
        conf = None
        if env_path:
            try:
                conf = DecoupleConfig(RepositoryEnv(env_path))
            except Exception as e:
                logging.warning(f"Failed to load env_path {env_path}: {e}")

        self.pubchem_bases: List[str] = []

        def _get(key, default=None):
            if conf is not None:
                return conf.get(key, default=default)
            return config(key, default=default)

        combined = _get("PUBCHEM_BASES", default=None)
        if combined:
            try:
                for part in str(combined).split(','):
                    s = part.strip()
                    if s:
                        self.pubchem_bases.append(s)
            except Exception:
                pass

        # Backwards-compatible: fall back to individual PUBCHEM_BASE_0 .. PUBCHEM_BASE_299
        if not self.pubchem_bases:
            for i in range(300):
                val = _get(f"PUBCHEM_BASE_{i}", default=None)
                if val is None:
                    continue
                if isinstance(val, bool):
                    continue
                try:
                    s = str(val).strip()
                except Exception:
                    continue
                if s:
                    self.pubchem_bases.append(s)

        # Initialize dump directory
        self.dump_dir = Path(config("DUMP_DIR"))
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"Using dump directory: {self.dump_dir}")

        # Set up session with rotating user agent
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": get_random_user_agent()})

        # Log configuration
        logging.info(f"PUBCHEM_BASES: {self.pubchem_bases}")

        # Scan local directory and compare with remote files
        local_files = self._scan_local_dumps()
        remote_files = self.list_current_full(skip_local=set(local_files))

        local_set = set(local_files)
        remote_set = set(remote_files)

        missing_files = remote_set - local_set
        if missing_files:
            logging.info(f"Found {len(missing_files)} files to download from NCBI")
            for fname in sorted(missing_files):
                logging.info(f"Missing file: {fname}")
        else:
            logging.info("No additional files found on NCBI - local directory is up to date")

    # ---------- Remote listing & download ----------
    def list_current_full(self, skip_local: Optional[Set[str]] = None) -> List[str]:
        """
        Return sorted list of `.sdf.gz` filenames available at all PUBCHEM_BASE URLs.

        Args:
            skip_local: optional set of filenames to skip (usually files already present
                        in `DUMP_DIR`) so we don't re-check them.

        This function ignores checksum files (e.g., `.md5`) and deduplicates filenames
        across multiple base URLs. It performs a single HEAD check per candidate file
        and will not re-traverse suffixes provided in `skip_local`.
        """
        all_files: Set[str] = set()
        # Only match the actual data files ending exactly with .sdf.gz
        pattern = re.compile(r'Compound_\d+_\d+\.sdf\.gz$')
        skip_local = skip_local or set()

        for base in self.pubchem_bases:
            logging.info(f"Traversing {base}")
            try:
                r = self.session.get(base, timeout=30)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                anchors = soup.find_all("a", href=True)
                found_any = False
                for a in anchors:
                    href = a["href"]
                    if not pattern.search(href):
                        continue
                    fname = Path(href).name
                    # Skip files we already have locally to avoid re-checking
                    if fname in skip_local:
                        continue
                    file_url = requests.compat.urljoin(base, href)
                    try:
                        head = self.session.head(file_url, timeout=10)
                        if head.status_code == 200:
                            if fname not in all_files:
                                all_files.add(fname)
                                logging.info(f"Accessible: {file_url}")
                            found_any = True
                        else:
                            logging.debug(f"Not accessible ({head.status_code}): {file_url}")
                    except Exception as e:
                        logging.debug(f"Failed to HEAD {file_url}: {e}")
                if not found_any:
                    # Fallback: try regex on raw text but skip local files
                    files = [f for f in pattern.findall(r.text) if f not in skip_local]
                    if files:
                        for f in files:
                            if f not in all_files:
                                all_files.add(f)
                                logging.info(f"Found (regex fallback): {f} at {base}")
                    else:
                        logging.debug(f"No dump links found at {base}")
            except Exception as e:
                logging.warning(f"Failed to list files from {base}: {e}")

        return sorted(all_files)

    def download_dump(self, fname_or_url: str, force: bool = False, chunk_size: int = 1024 * 1024) -> Path:
        """
        Download a dump file (fname or full URL) into dump_dir and return local Path.
        If the file already exists and force is False, the existing path is returned.
        """
        # Resolve URL and fname
        if fname_or_url.startswith("http://") or fname_or_url.startswith("https://"):
            url = fname_or_url
            fname = Path(url).name
        else:
            fname = Path(fname_or_url).name
            # Try to find which base contains this file
            url = None
            for base in self.pubchem_bases:
                test_url = base.rstrip('/') + '/' + fname
                try:
                    head = self.session.head(test_url, timeout=10)
                    if head.status_code == 200:
                        url = test_url
                        break
                except Exception:
                    continue
            if url is None:
                raise ValueError(f"Could not find {fname} in any PUBCHEM_BASE URLs.")

        out_path = self.dump_dir / fname
        if out_path.exists() and not force:
            logging.info(f"Already downloaded: {out_path}")
            return out_path

        tmp_path = out_path.with_name(out_path.name + ".part")
        logging.info(f"Downloading {url} -> {tmp_path}")
        try:
            with self.session.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                downloaded = 0
                with tmp_path.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                tmp_path.replace(out_path)
            logging.info(f"Saved to {out_path} ({downloaded} bytes)")
        except Exception as e:
            logging.error(f"Failed to download {url}: {e}")
            raise

        return out_path

    # ---------- Streaming helpers ----------
    def stream_dump_records(self, path_or_url: str) -> Iterable[Dict]:
        """
        Stream JSON lines (one JSON object per line) out of a .json.gz source.
        Accepts either a local path (string) or a URL.
        Yields Python dicts (records).
        """
        is_url = path_or_url.startswith("http://") or path_or_url.startswith("https://")
        if is_url:
            with self.session.get(path_or_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                r.raw.decode_content = True
                gzfile = gzip.GzipFile(fileobj=r.raw)
                for raw_line in gzfile:
                    try:
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        yield json.loads(line)
                    except Exception:
                        continue
        else:
            p = Path(path_or_url)
            if not p.exists():
                raise FileNotFoundError(f"{p} does not exist")
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        line = line.strip()
                        if not line:
                            continue
                        yield json.loads(line)
                    except Exception:
                        continue

    # ---------- Query helpers ----------
    @staticmethod
    def _extract_cid(record: Dict) -> Optional[int]:
        """Robustly extract numeric CID from record if present."""
        for key in ("CID", "cid", "Id", "id"):
            if key in record:
                try:
                    return int(record[key])
                except Exception:
                    try:
                        return int(str(record[key]))
                    except Exception:
                        return None
        return None

    def find_cids_in_dump(self, source: str, cids: Set[int], max_matches: Optional[int] = None) -> List[Dict]:
        """
        Scan a single dump (local path or URL) for any records whose CID is in `cids`.
        Returns a list of matched records (stops early if max_matches reached).
        """
        matches: List[Dict] = []
        for rec in self.stream_dump_records(source):
            cid = self._extract_cid(rec)
            if cid is None:
                continue
            if cid in cids:
                rec["CID"] = cid
                matches.append(rec)
                if max_matches and len(matches) >= max_matches:
                    break
        return matches

    def find_cids_across_local_dumps(
        self,
        cids: Set[int],
        max_matches: int = 1000,
        download_missing: bool = False,
        remote_limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        Scan local dumps in dump_dir for CIDs. If not enough matches found and download_missing is True,
        list remote files and download them one-by-one (in order) until max_matches are found or remote_limit reached.
        """
        matches: List[Dict] = []
        local_files = sorted(self.dump_dir.glob("*.sdf.gz"))
        for p in local_files:
            if len(matches) >= max_matches:
                break
            print(f"Scanning local {p}")
            found = self.find_cids_in_dump(str(p), cids, max_matches - len(matches))
            matches.extend(found)

        if len(matches) < max_matches and download_missing:
            remote_files = self.list_current_full()
            if remote_limit is not None:
                remote_files = remote_files[:remote_limit]
            for fname in remote_files:
                if len(matches) >= max_matches:
                    break
                local_path = self.dump_dir / fname
                if not local_path.exists():
                    try:
                        self.download_dump(fname)
                    except Exception as e:
                        print(f"Failed to download {fname}: {e}")
                        continue
                print(f"Scanning downloaded {local_path}")
                found = self.find_cids_in_dump(str(local_path), cids, max_matches - len(matches))
                matches.extend(found)

        return matches

    # ---------- Utilities ----------
    def sample_random_cids(self, n: int = 1000, min_cid: int = 1, max_cid: int = 2_000_000) -> Set[int]:
        """Return a set of n unique random CIDs in the inclusive interval [min_cid, max_cid]."""
        if n > (max_cid - min_cid + 1):
            raise ValueError("n is larger than the CID range")
        return set(random.sample(range(min_cid, max_cid + 1), n))

    def _scan_local_dumps(self) -> List[str]:
        """
        Scan the dump directory for existing .sdf.gz files.
        Returns a list of filenames (not full paths).
        """
        if not self.dump_dir.exists():
            logging.info(f"Dump directory does not exist: {self.dump_dir}")
            return []
        
        pattern = re.compile(r'Compound_\d+_\d+\.(?:sdf|asn|json)\.gz')
        local_files = []
        
        for item in self.dump_dir.iterdir():
            if item.is_file() and pattern.match(item.name):
                local_files.append(item.name)
        
        logging.info(f"Found {len(local_files)} existing files in {self.dump_dir}")
        return local_files

    @staticmethod
    def export_matches_to_json(matches: Iterable[Dict], out_path: str):
        """Export matched records to a JSON file (list of objects)."""
        p = Path(out_path)
        # This checks that the list of matches is not empty
        try:
            with p.open("w", encoding="utf-8") as fh:
                json.dump(list(matches), fh, indent=2)
            if isinstance(matches, list):
                logging.info(f"Wrote {len(matches)} records to {p}")
            elif matches is None:
                raise Exception('There were no matches obtained')
            
        except Exception as e:
            logging.error(f"Error writing to {p}: {e}")
            sys.exit(1)

    def download_all_current_full(self, files_or_force: Union[bool, List[str]] = False) -> Optional[List[Path]]:
        """
        Download *all* CURRENT-Full .json.gz chunks into dump_dir.
        If a file already exists and force=False, it is skipped.
        
        Args:
            files_or_force: Either a bool for force mode, or a list of specific files to download
                          (for parallel execution support).
        
        Returns:
            List of downloaded file paths, or None if files_or_force was a bool.
        """
        # Handle both single-file (from parallel) and full-list modes
        if isinstance(files_or_force, (list, tuple)):
            files = files_or_force
            force = False
        else:
            force = files_or_force
            files = self.list_current_full()
            logging.info(f"Found {len(files)} dump files to download.")
        
        results = []
        for fname in files:
            try:
                path = self.download_dump(fname, force=force)
                results.append(path)
            except Exception as e:
                logging.error(f"❌ Failed to download {fname}: {e}")
        
        # In full-list mode (not parallel), just print completion
        if not isinstance(files_or_force, (list, tuple)):
            logging.info("✅ Finished downloading all CURRENT-Full dumps.")
            return None
        
        return results  # Return paths for parallel mode