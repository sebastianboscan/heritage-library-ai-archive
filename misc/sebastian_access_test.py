import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config

response = requests.get(f"{wp_config.base_url()}/", auth=wp_config.auth(), timeout=30)
response_json = response.json()
print(response_json)
