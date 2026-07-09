from app.main import app


if __name__ == "__main__":
    import uvicorn

    from app.config.settings import CONFIG

    uvicorn.run("app.main:app", host="0.0.0.0", port=CONFIG["PORT"], log_level="info", workers=1)
