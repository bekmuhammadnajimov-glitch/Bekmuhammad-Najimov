import os

import uvicorn

if __name__ == "__main__":
    # Hosting platforms (Railway, etc.) tell us the port through $PORT.
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
