'''
Verifies the WordPress credentials in .env work, and that the account has the
edit_posts capability the upload step will eventually need.

Prints:  <status> <display name> <edit_posts>
A 200 with edit_posts True means the credentials are good.
'''
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config

r = requests.get(f"{wp_config.base_url()}/users/me", auth=wp_config.auth(),
                 timeout=30, params={"context": "edit"})
print(r.status_code, r.json().get("name"), r.json().get("capabilities", {}).get("edit_posts"))
