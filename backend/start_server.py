"""Dev server entry point — reads PORT from environment for preview tooling."""
import os

# Ensure CWD is the backend folder so relative paths (frontend/, structiq.db) resolve correctly
os.chdir(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=False)
