"""Expiring local GIF downloads, scoped independently of the API key."""
import hashlib
import hmac
import json
import re
import threading
import time
from pathlib import Path


class ResultStore:
    def __init__(self, directory, key, ttl=3600):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key = key.encode()
        self.ttl = ttl
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.cleanup()
        self.thread = threading.Thread(target=self._cleaner, daemon=True)
        self.thread.start()

    def _signature(self, job_id, expires):
        return hmac.new(self.key, f'gif-result:{job_id}:{expires}'.encode(), hashlib.sha256).hexdigest()

    def save(self, job_id, data, base_url):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid result ID')
        expires = int(time.time()) + self.ttl
        with self.lock:
            path = self.directory / (job_id + '.gif')
            path.write_bytes(data)
            path.chmod(0o600)
            metadata = self.directory / (job_id + '.json')
            metadata.write_text(json.dumps({'expires': expires}))
            metadata.chmod(0o600)
        return f'{base_url}/v1/images/results/{job_id}.gif?expires={expires}&signature={self._signature(job_id, expires)}'

    def read(self, job_id, expires, signature):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id) or not re.fullmatch(r'[0-9a-f]{64}', signature):
            return None
        if expires <= time.time() or expires > time.time() + self.ttl + 5:
            return None
        if not hmac.compare_digest(signature, self._signature(job_id, expires)):
            return None
        with self.lock:
            try:
                metadata = json.loads((self.directory / (job_id + '.json')).read_text())
                if metadata['expires'] < expires:
                    return None
                return (self.directory / (job_id + '.gif')).read_bytes()
            except (OSError, ValueError, KeyError):
                return None

    def cleanup(self):
        with self.lock:
            for metadata in self.directory.glob('*.json'):
                if not re.fullmatch(r'[0-9a-f]{32}', metadata.stem):
                    continue
                try:
                    expired = json.loads(metadata.read_text())['expires'] <= time.time()
                except (ValueError, KeyError, OSError):
                    expired = True
                if expired:
                    metadata.with_suffix('.gif').unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)

    def _cleaner(self):
        while not self.stopped.wait(60):
            self.cleanup()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
