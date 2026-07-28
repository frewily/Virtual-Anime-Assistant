import os


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.app:app",
        host=os.getenv("ASSISTANT_HOST", "127.0.0.1"),
        port=int(os.getenv("ASSISTANT_PORT", "8080")),
    )
