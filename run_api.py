"""
run_api.py
Launches the Apex Financial Autonomous Underwriting FastAPI service.
Executable via:
  python run_api.py
  python run_api.py --host 0.0.0.0 --port 8000 --reload
"""
import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Apex Financial Autonomous Underwriting API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for local development.")
    args = parser.parse_args()
    uvicorn.run("api.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
