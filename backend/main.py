import os

from api.app import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("ASSISTANT_HOST", "127.0.0.1"),
        port=int(os.getenv("ASSISTANT_PORT", "8080")),
    )
