from dotenv import load_dotenv
import os
import traceback

load_dotenv()
api_key = os.getenv('GEMINI_API_KEY')
print('GEMINI_API_KEY set:', bool(api_key))
if not api_key:
    print('No GEMINI_API_KEY found in environment.')
    raise SystemExit(1)

import google.genai as genai
client = genai.Client(api_key=api_key)
print('Client initialized for model default.')

try:
    models = client.models.list()
    print('Available models:')
    for m in models:
        name = getattr(m, 'name', None) or getattr(m, 'model', None) or str(m)
        print('-', name)
except Exception:
    traceback.print_exc()
