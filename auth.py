import requests
import sys

# --- Configuration ---
GNS3_SERVER = "http://127.0.0.1:3080" # Removed the /v3 here to make pathing easier below
GNS3_USER = "admin"
GNS3_PASSWORD = "admin" # v3 defaults to admin/admin

session = requests.Session()

# ==========================================
# 0. Authenticate and get the JWT Token
# ==========================================
print("Authenticating with GNS3 API...")
login_credentials = {
    "username": GNS3_USER,
    "password": GNS3_PASSWORD
}

# Request the token using form data
auth_response = session.post(f"{GNS3_SERVER}/v3/access/users/login", data=login_credentials)

if auth_response.status_code != 200:
    print(f"Authentication failed! Status: {auth_response.status_code}")
    print(auth_response.text)
    sys.exit(1)

# Extract the token from the response
token = auth_response.json().get("access_token")

# Inject the Bearer token into the session headers for all future requests
session.headers.update({"Authorization": f"Bearer {token}"})
print("Authentication successful!\n")

# ==========================================
# 1. Create the Project (Continue as normal)
# ==========================================
# Example: resp = session.post(f"{GNS3_SERVER}/v3/projects", json=payload)
