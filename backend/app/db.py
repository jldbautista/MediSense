
# Supabase client
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

# Explicit path 
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

@lru_cache
def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in backend/ .env")
    return create_client(url, key)
    