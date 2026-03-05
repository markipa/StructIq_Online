"""Dev server entry point — reads PORT from environment for preview tooling."""
import os
import uvicorn

port = int(os.environ.get("PORT", 8000))
uvicorn.run("main:app", host="127.0.0.1", port=port, reload=True)
