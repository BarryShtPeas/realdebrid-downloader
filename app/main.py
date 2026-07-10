from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse


app = FastAPI(title="Real-Debrid Downloader")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Real-Debrid Downloader</title>
        <style>
          body { font-family: system-ui, sans-serif; max-width: 42rem; margin: 3rem auto; padding: 0 1rem; }
          label, input, button { display: block; width: 100%; }
          input, button { box-sizing: border-box; font: inherit; margin-top: .5rem; padding: .75rem; }
          button { cursor: pointer; }
        </style>
      </head>
      <body>
        <h1>Real-Debrid Downloader</h1>
        <form method="post" action="/submit">
          <label>
            Hoster URL
            <input name="url" type="url" required autocomplete="off" placeholder="https://example.com/file">
          </label>
          <button type="submit">Submit</button>
        </form>
      </body>
    </html>
    """


@app.post("/submit", response_class=HTMLResponse)
async def submit(url: str = Form(...)) -> str:
    _ = url
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Real-Debrid Downloader</title>
      </head>
      <body>
        <h1>Real-Debrid Downloader</h1>
        <p>Real-Debrid and aria2 integration is not implemented in this scaffold yet.</p>
        <p><a href="/">Submit another link</a></p>
      </body>
    </html>
    """


def main() -> None:
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    log_level = os.getenv("APP_LOG_LEVEL", "info")
    uvicorn.run("app.main:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
