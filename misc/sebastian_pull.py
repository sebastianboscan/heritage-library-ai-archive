import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config

auth = wp_config.auth()

def get_all_media():
    page = 1
    # 1. Write a request where you get 100 images

    # 2.


url = f"{wp_config.base_url()}/"
response = requests.get(url, auth=auth, timeout=30)
response_json = response.json()
print(response_json)
