import os
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

class Config:
    # Postgres
    PG_HOST = os.getenv("PG_HOST", "localhost")
    PG_PORT = int(os.getenv("PG_PORT", 5432))
    PG_USER = os.getenv("PG_USER", "postgres")
    PG_PASSWORD = os.getenv("PG_PASSWORD", "postgrespassword")
    PG_DB = os.getenv("PG_DB", "pitch_pulse")

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

    # Debezium
    DEBEZIUM_URL = os.getenv("DEBEZIUM_URL", "http://localhost:8083")

    # Qdrant
    QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
    QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

    # App
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # API-Football
    API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
    API_FOOTBALL_BASE_URL = os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")
    API_FOOTBALL_DAILY_LIMIT = int(os.getenv("API_FOOTBALL_DAILY_LIMIT", 100))
    API_FOOTBALL_POLL_INTERVAL_SECONDS = int(os.getenv("API_FOOTBALL_POLL_INTERVAL_SECONDS", 70))

settings = Config()