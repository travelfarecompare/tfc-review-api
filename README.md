# Expert Reviews API (Google via Serper.dev)

## Endpoint
GET /reviews?title=Eiffel+Tower&n=6

## Setup
- Set `SERPER_API_KEY` from https://serper.dev
- Optional: `ALLOWED_ORIGIN=https://yourdomain.com`

## Run locally
```
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy to Railway
Use the `main.py`, `requirements.txt`, `Procfile` and set your environment variables.
