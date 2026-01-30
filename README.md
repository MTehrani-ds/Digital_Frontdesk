# Digital Frontdesk – Dental Clinic Demo

A customer-facing demo of a policy-driven, agentic front-desk assistant
for dental clinics.

## What it does
- Safely handles patient questions
- Blocks medical advice
- Collects callback details
- Creates staff callback tasks automatically
- Shows a staff inbox/dashboard

## Run (Codespaces / local)
```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
