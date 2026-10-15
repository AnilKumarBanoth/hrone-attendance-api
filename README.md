# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance management (punch-in, punch-out, regularization audit trails) and aggregation pipeline analytics (monthly summaries, department summaries, late-comer leaderboards with standard competition ranking, and gap-filled daily trends with 7-day moving averages).

## Requirements

- Python 3.11+
- MongoDB 6.0+ (Local instance, Docker container, or MongoDB Atlas M0 cluster)

## Setup & Running

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables (Optional):**
   The application reads `MONGO_URI` and `MONGO_DB` directly from environment variables, defaulting to `mongodb://localhost:27017` and `attendance_db` if not set.
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

4. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

5. **Start the API Server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive API documentation (Swagger UI) is accessible at `http://localhost:8000/docs`.

## Architecture & Features

- **Part A (`REVIEW.md`):** Complete defect review of the starter code covering edge cases in grace period calculations, time zones, overnight shifts, banker's rounding vs. half-up rounding, race conditions, and indexing.
- **Part B (`app/main.py`):**
  - All endpoints implemented strictly against `openapi.yaml`.
  - Atomic concurrency protections for punch-in (unique compound indexes) and punch-out / regularization (`find_one_and_update` with optimistic history locks).
  - Business rules R1 through R10 implemented with strict precision.
  - Analytics endpoints implemented entirely inside MongoDB aggregation pipelines using `$lookup`, `$densify`, and `$setWindowFields`.
- **Part C (`DECISIONS.md` & `/admin/explain/{endpoint}`):**
  - Database indexes created idempotently at startup.
  - All explained endpoints verified to run via `IXSCAN` without full collection scans (`COLLSCAN`).
  - Architectural decisions documented in `DECISIONS.md`.
