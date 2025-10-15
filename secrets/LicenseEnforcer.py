import os
import hashlib
import sys
import json
from pathlib import Path

"""
WARNING: This file is part of the software's license enforcement mechanism.
Removing, modifying, or tampering with this file constitutes a violation of the License Agreement.
Unauthorized modifications will result in immediate termination of software functionality.
"""


class LicenseEnforcer:
    def __init__(self):
        self.base_path = Path(__file__).parent.parent
        self.src_path = self.base_path / 'src'
        self.license_path = self.base_path / 'LICENSE'
        self.hash_file = self.base_path / '.secrets' / 'source_hashes.json'

    def calculate_file_hash(self, filepath):
        """Calculate SHA-256 hash of a file"""
        sha256_hash = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for byte_block in iter(lambda: f.read(4096), b''):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def store_source_hashes(self):
        """Store hashes of all Python files in src directory"""
        hashes = {}
        for path in self.src_path.rglob('*.py'):
            if path.is_file():
                hashes[str(path.relative_to(self.base_path))] = self.calculate_file_hash(path)
        
        with open(self.hash_file, 'w') as f:
            json.dump(hashes, f)

    def verify_source_integrity(self):
        """Verify source code hasn't been modified"""
        if not self.hash_file.exists():
            print("Error: Source hash file missing. Source code integrity cannot be verified.")
            sys.exit(1)

        with open(self.hash_file) as f:
            stored_hashes = json.load(f)

        for filepath, stored_hash in stored_hashes.items():
            full_path = self.base_path / filepath
            if not full_path.exists():
                print(f"Error: Source file {filepath} is missing.")
                sys.exit(1)
            current_hash = self.calculate_file_hash(full_path)
            if current_hash != stored_hash:
                print(f"Error: Source file {filepath} has been modified.")
                sys.exit(1)

    def check_license(self):
        """Verify license file exists and is valid"""
        if not self.license_path.exists():
            print("Error: License file not found.")
            sys.exit(1)
        
        # Add additional license validation logic here if needed
        # For example, check license content, expiration, etc.

    def enforce(self):
        """Main enforcement method"""
        self.check_license()
        self.verify_source_integrity()

# Create enforcer instance when module is imported
enforcer = LicenseEnforcer()

# This should be called before running any application code
def verify_license():
    enforcer.enforce()

# Optional: Store initial source hashes (run once during setup)
if __name__ == "__main__":
    enforcer.store_source_hashes()