"""
Core utilities for parallel execution and request customization.
"""
import functools
import logging
import multiprocessing
from .logging_utils import setup_logging
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar, cast

# Type var for decorator
F = TypeVar('F', bound=Callable[..., Any])

# Common desktop/mobile user agents for rotation
USER_AGENTS = [
    # Desktop Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
    # Desktop Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/117.0",
    # Desktop Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5.2 Safari/605.1.15",
    # Mobile (for variety)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.5938.60 Mobile Safari/537.36",
]

# Default number of concurrent decompression workers (safe default = 1)
DEFAULT_DECOMPRESS_WORKERS = int(
    # allow override via env var for quick tuning
    os.environ.get("DEFAULT_DECOMPRESS_WORKERS", "1")
)

def get_random_user_agent() -> str:
    """Return a random, realistic user agent string."""
    return random.choice(USER_AGENTS)

def get_system_concurrency() -> tuple[int, int]:
    """
    Get the number of CPU cores and threads available.
    Returns (cores, threads) tuple.
    """
    cpu_count = os.cpu_count() or 1
    # Check if we're running in a containerized env with CPU quota
    try:
        with open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us') as f:
            quota = int(f.read())
        with open('/sys/fs/cgroup/cpu/cpu.cfs_period_us') as f:
            period = int(f.read())
        if quota > 0 and period > 0:
            container_cpus = max(1, quota // period)
            cpu_count = min(cpu_count, container_cpus)
    except (FileNotFoundError, ValueError):
        pass

    thread_count = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else cpu_count
    return cpu_count, thread_count

def parallel(min_cores: int = 1, min_threads: int = 1, max_workers: int | None = None) -> Callable[[F], F]:
    """
    Decorator that runs a function with parallel execution if enough cores/threads are available.
    Logs system concurrency on first use. Uses n-1 cores/threads to avoid overload.
    
    Args:
        min_cores: Minimum number of CPU cores required
        min_threads: Minimum number of threads required
    
    Usage:
        @parallel(min_cores=2, min_threads=2)
        def my_func(items): ...
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Set up logging first
            setup_logging('pubchem_core')
            
            cores, threads = get_system_concurrency()
            
            # Log available concurrency (once) 
            if not hasattr(wrapper, '_logged_concurrency'):
                logging.info(f"System has {cores} CPU cores and {threads} threads available")
                wrapper._logged_concurrency = True  # type: ignore
            
            if cores < min_cores or threads < min_threads:
                logging.error(
                    f"Insufficient system resources. Need at least {min_cores} cores "
                    f"and {min_threads} threads, but found {cores} cores and {threads} threads."
                )
                return None
            
            # Use n-1 workers to avoid overload unless a max_workers override provided
            if max_workers is None:
                worker_count = max(1, min(cores - 1, threads - 1))
            else:
                worker_count = max(1, int(max_workers))
            
            # If the first arg is a list/tuple/set, assume we're processing items in parallel
            if args and isinstance(args[0], (list, tuple, set)):
                items = args[0]
                other_args = args[1:]
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [
                        executor.submit(func, [item], *other_args, **kwargs)
                        for item in items
                    ]
                    results = []
                    for f in futures:
                        try:
                            result = f.result()
                            if result is not None:
                                results.extend(result if isinstance(result, (list, tuple)) else [result])
                        except Exception as e:
                            logging.error(f"Worker failed: {e}")
                    return results
            
            # If no iterable passed, just run the function directly
            return func(*args, **kwargs)
        
        return cast(F, wrapper)
    return decorator


def decompress_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """Create a ThreadPoolExecutor for decompression tasks.

    Uses DEFAULT_DECOMPRESS_WORKERS by default, but callers may pass max_workers
    to briefly override the global default for a specific run.
    """
    workers = DEFAULT_DECOMPRESS_WORKERS if max_workers is None else int(max_workers)
    return ThreadPoolExecutor(max_workers=workers)
