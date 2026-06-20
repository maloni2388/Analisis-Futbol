services:
  - type: web
    name: pizarra-backend
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn backend:app
    envVars:
      - key: FOOTBALL_DATA_TOKEN
        sync: false
      - key: APISPORTS_KEY
        sync: false
